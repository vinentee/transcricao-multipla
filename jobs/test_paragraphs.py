import json
import uuid

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AudioJob, Speaker, TranscriptSegment
from .paragraphs import group_speaker_turns
from .services import save_provider_segments


def utterance(speaker, start, end, text, **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, speaker_scope='global' if speaker is not None else 'unknown', **extra)


class SpeakerTurnTests(SimpleTestCase):
    def test_consecutive_speaker_zero_long_pause_and_brief_intervention(self):
        source = [utterance('0', 0, 1, 'Olá.'), utterance('0', 100, 101, 'Continuando.'),
                  utterance('1', 102, 102.1, 'Sim.'), utterance('0', 103, 104, 'Obrigado.')]
        grouped = group_speaker_turns(source)
        self.assertEqual([s['text'] for s in grouped], ['Olá. Continuando.', 'Sim.', 'Obrigado.'])
        self.assertEqual((grouped[0]['start'], grouped[0]['end']), (0, 101))
        self.assertEqual(len(grouped[0]['source_segments']), 2)
        self.assertNotIn('source_segments', source[0])

    def test_unknown_local_and_crossing_overlaps_are_barriers(self):
        source = [utterance(None, 0, 1, 'A'), utterance(None, 1, 2, 'B'),
                  dict(utterance('block1:A', 2, 3, 'C'), speaker_scope='local'),
                  dict(utterance('block1:A', 3, 4, 'D'), speaker_scope='local'),
                  utterance('0', 4, 10, 'E'), utterance('1', 5, 6, 'F'),
                  utterance('1', 7, 8, 'G'), utterance('1', 11, 12, 'H')]
        self.assertEqual(len(group_speaker_turns(source)), len(source))

    def test_preserves_raw_words_alternatives_corrections_and_issue_intervals(self):
        source = [utterance('0', 0, 1, 'Obrigado.', raw_text='Thank you.', corrected_text='Obrigado.',
                           alternatives=[{'text': 'Obrigado.', 'applied': True}], review_reasons=['verification_unresolved'], words=[{'word': 'Thank', 'start': 0, 'end': .4}]),
                  utterance('0', 2, 3, 'Até logo.', words=[])]
        turn = group_speaker_turns(source)[0]
        self.assertEqual(turn['raw_text'], 'Thank you. Até logo.')
        self.assertEqual(turn['corrected_text'], 'Obrigado. Até logo.')
        self.assertEqual(turn['source_segments'][0]['alternatives'], source[0]['alternatives'])
        self.assertEqual(turn['source_segments'][0]['end'], 1)
        self.assertEqual(turn['words'], source[0]['words'])


class StructuralReviewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='structure@example.com', password='password')
        self.client.force_login(self.user)
        self.job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.READY, original_filename='audio.wav', original_key='structure.wav', duration_seconds=60)
        self.speaker = Speaker.objects.create(job=self.job, number=1, display_name='Pessoa 1')
        self.other_speaker = Speaker.objects.create(job=self.job, number=2, display_name='Pessoa 2')
        self.source = TranscriptSegment.objects.create(job=self.job, ordinal=1, start_ms=1000, end_ms=9000,
            raw_text='Primeira versão', revised_text='Bom dia. Tudo bem?', has_manual_revision=True, speaker=self.speaker,
            needs_review=True, review_reasons=['english'], alternatives=[{'text': 'Outra versão'}],
            source_segments=[{'start': 1, 'end': 3, 'review_reasons': ['english']}])
        self.later = TranscriptSegment.objects.create(job=self.job, ordinal=2, start_ms=15000, end_ms=20000,
            raw_text='Outro trecho.', speaker=self.speaker)
        self.url = reverse('save_review', args=[self.job.pk])

    def part(self, start=1000, end=4000, text='Bom dia.', **extra):
        return dict(id='draft_' + str(uuid.uuid4()), start_ms=start, end_ms=end, text=text, speaker=str(self.speaker.pk), **extra)

    def replacement(self, parts=None):
        return {'source_id': str(self.source.pk), 'parts': parts or [self.part(), self.part(4000, 9000, 'Tudo bem?')]}

    def post(self, replacements=None, additions=None, version='0', **extra):
        payload = {'review_version': version, 'structure_changes': json.dumps({'replacements': replacements or [], 'additions': additions or []})}
        payload.update(extra)
        return self.client.post(self.url, payload, HTTP_ACCEPT='application/json')

    def active(self):
        return list(self.job.segments.filter(superseded_at__isnull=True, excluded_at__isnull=True))

    def test_split_preserves_audit_distributes_issues_and_exports_active_parts(self):
        parts = [self.part(), self.part(4000, 9000, 'Tudo bem?')]
        parts[1]['speaker'] = str(self.other_speaker.pk)
        response = self.post([self.replacement(parts)], **{f'segment_{self.later.pk}': 'Outra edição preservada'})
        self.assertEqual(response.status_code, 200, response.content)
        self.source.refresh_from_db()
        self.job.refresh_from_db()
        self.assertIsNotNone(self.source.superseded_at)
        self.assertIsNone(self.source.excluded_at)
        self.assertEqual(self.source.raw_text, 'Primeira versão')
        self.assertEqual(self.source.text, 'Bom dia. Tudo bem?')
        self.assertTrue(self.source.alternatives)
        self.assertEqual(self.job.review_version, 1)
        children = self.source.split_parts.order_by('start_ms')
        self.assertEqual([s.text for s in children], ['Bom dia.', 'Tudo bem?'])
        self.assertEqual([s.review_reasons for s in children], [['english'], []])
        self.assertEqual(children[1].speaker, self.other_speaker)
        self.assertEqual(children[0].raw_text, '')
        self.later.refresh_from_db()
        self.assertEqual(self.later.text, 'Outra edição preservada')
        self.assertEqual(len(self.active()), 3)
        for fmt in ('txt', 'srt', 'pdf'):
            body = self.client.get(reverse('export', args=[self.job.pk, fmt])).content
            self.assertNotIn(b'Primeira', body)
            self.assertIn(b'Bom dia.', body)
        detail = self.client.get(reverse('job_detail', args=[self.job.pk]))
        self.assertEqual(len(detail.context['segments']), 3)
        self.assertEqual(len(detail.context['superseded_segments']), 1)
        self.assertEqual(len(detail.context['excluded_segments']), 0)

    def test_three_parts_and_nested_split_keep_warning_localized(self):
        parts = [self.part(1000, 2000, 'Bom'), self.part(2000, 4000, 'dia.'), self.part(4000, 9000, 'Tudo bem?')]
        self.assertEqual(self.post([self.replacement(parts)]).status_code, 200)
        child = self.source.split_parts.get(start_ms=2000)
        again = {'source_id': str(child.pk), 'parts': [self.part(2000, 3000, 'di'), self.part(3000, 4000, 'a.')]}
        self.assertEqual(self.post([again], version='1').status_code, 200)
        self.assertEqual([s.review_reasons for s in child.split_parts.order_by('start_ms')], [['english'], []])

    def test_insert_new_speaker_and_confirm_review_explicitly(self):
        part = self.part(10000, 12000, 'Fala omitida.')
        part['speaker'] = 'new_1'
        response = self.post(additions=[part], new_speaker_new_1='Nova pessoa')
        self.assertEqual(response.status_code, 200, response.content)
        manual = self.job.segments.get(origin='manual')
        self.assertEqual(manual.speaker.display_name, 'Nova pessoa')
        self.assertEqual(manual.review_reasons, ['manual_check'])
        self.assertEqual(manual.raw_text, '')
        self.assertEqual([s.start_ms for s in self.active()], [1000, 10000, 15000])

    def test_overlap_requires_explicit_confirmation_even_with_other_additions(self):
        part = self.part(2000, 5000, 'Intervenção.')
        self.assertEqual(self.post(additions=[part]).status_code, 400)
        part['overlap_confirmed'] = True
        self.assertEqual(self.post(additions=[part]).status_code, 200)
        self.assertEqual(len(self.active()), 3)

    def test_invalid_batch_does_not_save_text_names_or_partial_structure(self):
        parts = [self.part(), self.part(5000, 9000, 'Lacuna')]
        response = self.post([self.replacement(parts)], **{f'segment_{self.later.pk}': 'Não salvar', f'speaker_{self.speaker.pk}': 'Não renomear', 'new_speaker_new_1': 'Não criar'})
        self.assertEqual(response.status_code, 400)
        self.later.refresh_from_db(); self.speaker.refresh_from_db()
        self.assertEqual(self.later.revised_text, '')
        self.assertEqual(self.speaker.display_name, 'Pessoa 1')
        self.assertEqual(self.job.speakers.count(), 2)
        self.assertEqual(self.job.segments.count(), 2)

    def test_invalid_timestamps_ids_and_speakers_are_rejected(self):
        foreign_job = AudioJob.objects.create(user=self.user, original_key='other.wav', original_filename='other.wav')
        foreign = Speaker.objects.create(job=foreign_job, number=1, display_name='Pessoa externa')
        cases = [dict(start_ms=-1), dict(end_ms=61000), dict(start_ms=True), dict(end_ms=1000), dict(text=None), dict(text=' '), dict(speaker=str(foreign.pk)), dict(id='invalid')]
        for values in cases:
            with self.subTest(values=values):
                part = self.part(10000, 12000, 'Teste'); part.update(values)
                self.assertEqual(self.post(additions=[part]).status_code, 400)
        self.assertEqual(self.job.segments.count(), 2)

    def test_conflict_preserves_newer_revision(self):
        self.assertEqual(self.post(**{f'segment_{self.later.pk}': 'Nova edição'}).status_code, 200)
        response = self.post([self.replacement()])
        self.assertEqual(response.status_code, 409)
        self.later.refresh_from_db()
        self.assertEqual(self.later.text, 'Nova edição')
        self.assertEqual(self.job.segments.count(), 2)

    def test_deleted_or_superseded_source_cannot_be_split_or_restored_as_active(self):
        self.source.excluded_at = timezone.now(); self.source.save()
        self.assertEqual(self.post([self.replacement()]).status_code, 400)
        self.source.excluded_at = None; self.source.save()
        self.assertEqual(self.post([self.replacement()]).status_code, 200)
        self.assertEqual(self.post([self.replacement()], version='1').status_code, 400)
        self.assertEqual(self.post(version='1', **{f'restore_segment_{self.source.pk}': '1'}).status_code, 200)
        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.superseded_at)

    def test_other_user_and_unavailable_job_are_blocked(self):
        other = get_user_model().objects.create_user(email='outsider-structure@example.com', password='password')
        self.client.force_login(other)
        self.assertEqual(self.post(additions=[self.part()]).status_code, 404)
        self.client.force_login(self.user)
        for status in (AudioJob.Status.PROCESSING, AudioJob.Status.FAILED):
            self.job.status = status; self.job.save()
            self.assertEqual(self.post(additions=[self.part()]).status_code, 404)

    def test_long_audio_timestamps_and_split_deletion_keep_coverage_and_history(self):
        self.job.duration_seconds = 7200
        self.job.save(update_fields=['duration_seconds'])
        part = self.part(4700000, 4710000, 'Trecho no final da gravação.')
        part['reviewed'] = True
        self.assertEqual(self.post(additions=[part]).status_code, 200)
        manual = self.job.segments.get(origin='manual')
        self.assertFalse(manual.needs_review)
        parts = [self.part(), self.part(4000, 9000, 'Tudo bem?')]
        parts[0]['deleted'] = True
        self.assertEqual(self.post([self.replacement(parts)], version='1').status_code, 200)
        self.assertEqual(self.source.split_parts.count(), 2)
        self.assertEqual(self.source.split_parts.filter(excluded_at__isnull=False).count(), 1)
        self.assertIn('01:18:20,000', self.client.get(reverse('export', args=[self.job.pk, 'srt'])).content.decode())

    def test_structural_changes_require_version_and_unique_parts(self):
        response = self.client.post(self.url, {'structure_changes': json.dumps({'additions': [self.part(10000, 12000)]})})
        self.assertEqual(response.status_code, 400)
        value = self.part(10000, 12000)
        self.assertEqual(self.post(additions=[value, value]).status_code, 400)
        self.assertEqual(self.job.segments.count(), 2)

    def test_new_grouping_is_opt_in_existing_rows_are_unchanged(self):
        new_job = AudioJob.objects.create(user=self.user, original_key='new.wav', original_filename='new.wav')
        save_provider_segments(new_job, [utterance('0', 30, 31, 'Um.'), utterance('0', 32, 33, 'Dois.')], group_turns=True)
        self.assertEqual(new_job.segments.count(), 1)
        self.assertEqual(new_job.segments.get().text, 'Um. Dois.')
        self.assertEqual(new_job.segments.get().origin, 'grouped')
        self.assertEqual(self.job.segments.count(), 2)
