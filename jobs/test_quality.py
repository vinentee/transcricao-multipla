import os
import tempfile
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .audio import AudioChunk, SpeakerReferences, boundaries, cut_pcm, prepare_chunks, voice_intervals
from .models import AudioJob, Quote
from .providers import OpenAITranscriptionProvider
from .quality import assess, choose_retry, merge_chunks
from .services import save_provider_segments
from .tasks import transcribe_job
from payments.models import Payment


def segment(text="Olá, como você está?", start=0, end=3, speaker="A"):
    return {"text": text, "start": start, "end": end, "speaker": speaker, "review_reasons": []}


def write_wave(path, seconds):
    with wave.open(path, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * round(seconds * 16000))


class QualityTests(SimpleTestCase):
    def test_english_and_short_foreign_words(self):
        text = "Thank you very much for taking the time to talk with us today about the important changes that we need to make in our work together."
        result = assess([segment(text)], 4, [(0, 3)])
        self.assertIn("english", result[0][0]["review_reasons"])
        self.assertEqual(result, assess([segment(text)], 4, [(0, 3)]))
        self.assertEqual(assess([segment("Obrigado, John, pelo feedback.")], 4, [(0, 3)])[1], [])

    def test_repetition_and_uncovered_voice(self):
        result = assess([segment("vamos conversar sobre isso " * 4, end=2)], 8, [(0, 2), (4, 7)])
        self.assertEqual({i["code"] for i in result[1]}, {"repetition", "missing_speech"})
        self.assertEqual(result[2], 3)
        self.assertFalse(assess([], 8, [])[1])
        self.assertEqual(assess([segment("1234")], 3, [(0, 3)])[1], [])

    def test_invalid_times_preserve_text_but_do_not_hide_gaps(self):
        result = assess([segment(start=float("nan"))], 5, [(0, 5)])
        self.assertEqual(result[0][0]["text"], "Olá, como você está?")
        self.assertEqual(result[2], 5)
        self.assertIn("invalid_segment", result[0][0]["review_reasons"])

    def test_retry_only_wins_with_fewer_problems_and_no_coverage_loss(self):
        original = assess([segment("vamos conversar sobre isso " * 4)], 4, [(0, 3)])
        good = assess([segment()], 4, [(0, 3)])
        self.assertIs(choose_retry(original, good), good)
        self.assertIs(choose_retry(original, original), original)
        missing = assess([segment(end=1)], 4, [(0, 3)])
        self.assertIs(choose_retry(original, missing), original)

    def test_merge_preserves_simultaneous_short_speech_and_order(self):
        chunks = [(0, 10, [segment(start=5, end=9, speaker="voice_1"), segment("Sim", 6, 7, "voice_2"), segment("Primeiro", 0, 1)])]
        merged = merge_chunks(chunks)
        self.assertEqual([s["text"] for s in merged], ["Primeiro", "Olá, como você está?", "Sim"])
        self.assertEqual(merged[2]["start"], 6)

    def test_merge_deduplicates_only_confirmed_matching_neighbor(self):
        old = segment("Uma frase inteira.", 8, 10, "voice_1")
        same = segment("Uma frase inteira!", 1, 3, "voice_1")
        self.assertEqual(len(merge_chunks([(0, 10, [old]), (7, 10, [same])])), 1)
        same["speaker"] = "voice_2"
        self.assertEqual(len(merge_chunks([(0, 10, [old]), (7, 10, [same])])), 2)
        same["speaker"] = None
        merged = merge_chunks([(0, 10, [old]), (7, 10, [same])])
        self.assertTrue(all("boundary_conflict" in s["review_reasons"] for s in merged))
        # Repetition elsewhere in the file is not a duplicate.
        self.assertEqual(len(merge_chunks([(0, 10, [old]), (10, 10, [segment("Uma frase inteira.", 0, 2, "voice_1")])])), 2)

    def test_numeric_differences_are_not_deduplicated(self):
        merged = merge_chunks([(0, 10, [segment("500 reais", 8, 10, "voice_1")]), (7, 10, [segment("600 reais", 1, 3, "voice_1")])])
        self.assertEqual(len(merged), 2)

    def test_partial_boundary_versions_are_preserved(self):
        merged = merge_chunks([(0, 10, [segment("Uma frase", 8, 10, "voice_1")]), (7, 10, [segment("Uma frase inteira", 1, 4, "voice_1")])])
        self.assertEqual(len(merged), 2)
        self.assertIn("boundary_conflict", merged[1]["review_reasons"])

    def test_pause_boundaries_and_long_audio_have_no_gaps(self):
        self.assertEqual(list(boundaries(500, [(0, 233), (235, 500)], 240, 3))[0], (0, 235))
        cuts = list(boundaries(7200, [(0, 7200)], 240, 3))
        self.assertEqual(cuts[-1][1], 7200)
        self.assertTrue(all(b[0] == a[1] - 3 for a, b in zip(cuts, cuts[1:])))
        self.assertEqual(list(boundaries(4, [], 240, 3)), [(0, 4)])
        for size, overlap in [(0, 3), (240, -1), (240, 240), (240, 235)]:
            with self.assertRaises(ValueError):
                list(boundaries(500, [], size, overlap))

    def test_pcm_cuts_preserve_samples_and_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = os.path.join(directory, "source.wav"), os.path.join(directory, "cut.wav")
            write_wave(source, 3)
            cut_pcm(source, target, 1, 2)
            with wave.open(target) as audio:
                self.assertEqual(audio.getnframes(), 32000)
                self.assertEqual(audio.readframes(32000), b"\0\0" * 32000)
            self.assertEqual(voice_intervals(source), [])

    def test_vad_keeps_short_tail_and_uses_conservative_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tail.wav")
            write_wave(path, .04)
            with patch("webrtcvad.Vad") as vad:
                vad.return_value.is_speech.return_value = True
                intervals = voice_intervals(path)
                vad.assert_called_once_with(1)
                self.assertAlmostEqual(intervals[0][1], .04)

    def test_temporary_files_removed_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            write_wave(os.path.join(directory, "input.wav"), 2)
            storage = FileSystemStorage(location=directory)
            job = SimpleNamespace(original_filename="input.wav", original_key="input.wav")
            with patch("jobs.audio.default_storage", storage):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    with prepare_chunks(job) as chunks:
                        workdir = os.path.dirname(chunks[0].path)
                        self.assertTrue(os.path.exists(chunks[0].path))
                        self.assertEqual(chunks[0].duration, 2)
                        raise RuntimeError("stop")
                self.assertFalse(os.path.exists(workdir))

    @override_settings(TRANSCRIPTION_LANGUAGE="pt")
    def test_provider_sends_language_and_supported_reference_parameters(self):
        with tempfile.NamedTemporaryFile() as audio, patch("openai.OpenAI") as client:
            client.return_value.audio.transcriptions.create.return_value = SimpleNamespace(segments=[], usage={"input_tokens": 8}, duration=3)
            known = {"known_speaker_names": ["voice_1"], "known_speaker_references": ["data:audio/wav;base64,test"]}
            OpenAITranscriptionProvider().transcribe(audio.name, known)
            args = client.return_value.audio.transcriptions.create.call_args.kwargs
            self.assertEqual(args["language"], "pt")
            self.assertEqual(args["extra_body"], known)
            self.assertEqual(args["response_format"], "diarized_json")
            self.assertNotIn("prompt", args)

    def test_reference_identity_is_not_inferred_from_local_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.wav")
            write_wave(path, 5)
            chunk = AudioChunk(path, 0, 5, [(0, 5)], path)
            with SpeakerReferences() as refs:
                first = [segment(end=3)]
                refs.identify(first, chunk)
                self.assertEqual(first[0]["speaker"], "voice_1")
                self.assertEqual(refs.payload()["known_speaker_names"], ["voice_1"])
                self.assertTrue(refs.payload()["known_speaker_references"][0].startswith("data:audio/wav;base64,"))
                changed = [segment(end=3, speaker="voice_1")]
                refs.identify(changed, chunk)
                self.assertEqual(changed[0]["speaker"], "voice_1")
                # No source/reference available: raw A is not voice_1.
                unknown = [segment()]
                refs.identify(unknown, AudioChunk(path, 0, 5), allow_new=False)
                self.assertIsNone(unknown[0]["speaker"])
                self.assertIn("unknown_speaker", unknown[0]["review_reasons"])
            self.assertFalse(refs.references)
            self.assertEqual(os.listdir(directory), ["source.wav"])

    def test_reference_memory_cleared_after_exception(self):
        refs = SpeakerReferences()
        with self.assertRaises(RuntimeError):
            with refs:
                refs.references["voice_1"] = "temporary sample"
                raise RuntimeError("stop")
        self.assertFalse(refs.references)

    def test_no_reference_from_overlapping_voices_or_more_than_four(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.wav")
            write_wave(path, 20)
            refs = SpeakerReferences()
            chunk = AudioChunk(path, 0, 20, [(0, 20)], path)
            overlap = [segment(end=3), segment(start=1, end=4, speaker="B")]
            refs.identify(overlap, chunk)
            self.assertFalse(refs.references)
            many = [segment(start=i * 3, end=i * 3 + 2, speaker=str(i)) for i in range(6)]
            refs.identify(many, chunk)
            self.assertEqual(len(refs.references), 4)
            self.assertEqual(len(many), 6)
            self.assertIsNone(many[-1]["speaker"])


@override_settings(DEV_BYPASS_PAYMENT=True)
class QualityIntegrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="quality@example.com", password="safe-test-password")
        self.job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.PAID_QUEUED, original_key="quality.wav", original_filename="quality.wav", duration_seconds=10)
        quote = Quote.objects.create(job=self.job, duration_seconds=10, total_brl="10", api_brl="1", infra_brl="1", pricing_snapshot={}, expires_at=timezone.now() + timezone.timedelta(minutes=30))
        Payment.objects.create(job=self.job, status=Payment.Status.RECEIVED, amount_brl=quote.total_brl)

    def run_worker(self, results):
        chunks = MagicMock()
        chunks.__enter__.return_value = [AudioChunk("/tmp/mocked.wav", 0, 10, [(0, 3)])]
        with patch("jobs.tasks.prepared_transcription_chunks", return_value=chunks), patch("jobs.tasks.default_storage.exists", return_value=True), patch("jobs.tasks.OpenAITranscriptionProvider") as provider:
            provider.return_value.transcribe.side_effect = results
            transcribe_job.run(str(self.job.id))
            self.job.refresh_from_db()
            return provider.return_value.transcribe.call_count

    def test_retry_wins_usage_accumulates_charge_unchanged(self):
        bad = {"segments": [segment("vamos conversar sobre isso " * 4)], "usage": {"input_tokens": 10, "output_tokens": 20}}
        good = {"segments": [segment()], "usage": SimpleNamespace(input_tokens=11, output_tokens=12)}
        self.assertEqual(self.run_worker([bad, good]), 2)
        self.assertEqual(self.job.status, AudioJob.Status.READY)
        self.assertEqual(self.job.segments.get().raw_text, "Olá, como você está?")
        self.assertEqual(self.job.usage.input_tokens, 21)
        self.assertEqual(self.job.usage.output_tokens, 32)
        self.assertEqual(self.job.usage.charged_brl, 10)
        self.assertEqual(self.job.quality_metadata["retries"], 1)

    def test_retry_failure_preserves_original_and_flags_review(self):
        result = {"segments": [segment("vamos conversar sobre isso " * 4)]}
        self.assertEqual(self.run_worker([result, TimeoutError("provider failure")]), 2)
        self.assertEqual(self.job.status, AudioJob.Status.READY)
        self.assertIn("retry_failed", self.job.segments.get().review_reasons)
        self.assertTrue(self.job.needs_review)

    def test_tied_retry_is_limited_to_one_and_no_false_speaker_identity(self):
        result = {"segments": [segment("vamos conversar sobre isso " * 4)]}
        self.assertEqual(self.run_worker([result, result]), 2)
        saved = self.job.segments.get()
        self.assertIsNone(saved.speaker)
        self.assertIn("retry_unresolved", saved.review_reasons)
        self.assertEqual(saved.raw_text, result["segments"][0]["text"])

    def test_followup_calls_receive_references_and_sum_duration_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "source.wav")
            write_wave(path, 10)
            chunks = MagicMock()
            chunks.__enter__.return_value = [AudioChunk(path, 0, 6, [(0, 3)], path), AudioChunk(path, 3, 7, [(0, 3)], path)]
            with patch("jobs.tasks.prepared_transcription_chunks", return_value=chunks), patch("jobs.tasks.default_storage.exists", return_value=True), patch("jobs.tasks.OpenAITranscriptionProvider") as provider:
                provider.return_value.transcribe.side_effect = [
                    {"segments": [segment()], "usage": {"seconds": 6}},
                    {"segments": [segment(speaker="voice_1")], "usage": {"seconds": 7}},
                ]
                transcribe_job.run(str(self.job.id))
                self.assertEqual(provider.return_value.transcribe.call_count, 2)
                known = provider.return_value.transcribe.call_args.kwargs["known_speakers"]
                self.assertEqual(known["known_speaker_names"], ["voice_1"])
            self.job.refresh_from_db()
            self.assertEqual(self.job.speakers.count(), 1)
            self.assertEqual(self.job.segments.count(), 2)
            self.assertFalse(self.job.needs_review)
            self.assertEqual(self.job.quality_metadata["provider_audio_seconds"], 13)

    def test_speaker_limit_does_not_discard_text(self):
        many = [segment(start=i, end=i + 1, speaker=f"voice_{i}") for i in range(6)]
        save_provider_segments(self.job, many)
        self.assertEqual(self.job.segments.count(), 6)
        self.assertEqual(self.job.speakers.count(), 6)

    def test_review_flags_exports_and_explicit_confirmation(self):
        self.run_worker([{"segments": []}, {"segments": []}])
        self.client.force_login(self.user)
        detail = self.client.get(reverse("job_detail", args=[self.job.id]))
        self.assertContains(detail, "Voz sem transcrição correspondente")
        self.assertContains(detail, "00:00:00 - 00:00:03")
        for fmt in ("txt", "srt", "pdf"):
            result = self.client.get(reverse("export", args=[self.job.id, fmt]))
            self.assertEqual(result.status_code, 200)
            if fmt != "pdf":
                self.assertIn("revisão pendente", result.content.decode().lower())
            else:
                self.assertIn(b"REVIS", result.content)
        self.client.post(reverse("save_review", args=[self.job.id]), {"confirm_quality_review": "1"})
        self.job.refresh_from_db()
        self.assertFalse(self.job.needs_review)

    def test_saving_text_does_not_implicitly_clear_review(self):
        self.job.status, self.job.needs_review = AudioJob.Status.READY, True
        self.job.save()
        save_provider_segments(self.job, [dict(segment(), speaker=None, review_reasons=["unknown_speaker"])])
        saved = self.job.segments.get()
        self.client.force_login(self.user)
        self.client.post(reverse("save_review", args=[self.job.id]), {f"segment_{saved.id}": "Texto conferido"})
        saved.refresh_from_db()
        self.assertEqual(saved.revised_text, "Texto conferido")
        self.assertTrue(saved.needs_review)
