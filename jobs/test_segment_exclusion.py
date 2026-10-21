from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AudioJob, Speaker, TranscriptSegment


class SegmentExclusionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="exclude@example.com", password="test-password")
        self.other = get_user_model().objects.create_user(email="other-exclude@example.com", password="test-password")
        self.job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.READY, original_key="original-kept.wav", original_filename="original.wav", duration_seconds=20, needs_review=True)
        speaker = Speaker.objects.create(job=self.job, number=1, display_name="Ana")
        self.first = TranscriptSegment.objects.create(job=self.job, speaker=speaker, ordinal=1, start_ms=1000, end_ms=3000, raw_text="REMOVE_THIS_HALLUCINATION", revised_text="REMOVE_THIS_EDIT", has_manual_revision=True,
                                                     needs_review=True, review_reasons=["english"], alternatives=[{"text": "alternative kept", "applied": False}])
        self.second = TranscriptSegment.objects.create(job=self.job, speaker=speaker, ordinal=2, start_ms=5000, end_ms=8000, raw_text="KEEP_THIS_REAL_SPEECH")
        self.job.review_reasons = [{"code": "english", "start": 1, "end": 3}]
        self.job.save()
        self.client.force_login(self.user)
        self.save_url = reverse("save_review", args=[self.job.pk])

    def exclude(self):
        return self.client.post(self.save_url, {f"delete_segment_{self.first.pk}": "1"})

    def test_exclusion_preserves_history_audio_and_other_timestamps(self):
        response = self.exclude()
        self.assertEqual(response.status_code, 302)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.job.refresh_from_db()
        self.assertIsNotNone(self.first.excluded_at)
        self.assertEqual(self.first.raw_text, "REMOVE_THIS_HALLUCINATION")
        self.assertEqual(self.first.revised_text, "REMOVE_THIS_EDIT")
        self.assertEqual(self.first.alternatives[0]["text"], "alternative kept")
        self.assertEqual(self.first.review_reasons, ["english"])
        self.assertIsNone(self.second.excluded_at)
        self.assertEqual((self.second.start_ms, self.second.end_ms, self.second.ordinal), (5000, 8000, 2))
        self.assertEqual(self.job.original_key, "original-kept.wav")
        self.assertIsNone(self.job.audio_deleted_at)
        self.assertFalse(self.job.needs_review)
        self.assertEqual(self.job.segments.count(), 2)

    def test_all_exports_omit_excluded_content_and_keep_cue_times(self):
        self.exclude()
        for fmt in ("txt", "srt", "pdf"):
            response = self.client.get(reverse("export", args=[self.job.pk, fmt]))
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b"REMOVE_THIS", response.content)
            self.assertIn(b"KEEP_THIS_REAL_SPEECH", response.content)
        srt = self.client.get(reverse("export", args=[self.job.pk, "srt"])).content.decode()
        self.assertTrue(srt.startswith("1\n00:00:05,000 --> 00:00:08,000"))
        self.assertNotIn("Revisão pendente", srt)

    def test_detail_separates_archived_segments_and_restores_on_save(self):
        self.exclude()
        response = self.client.get(reverse("job_detail", args=[self.job.pk]))
        self.assertEqual([s.pk for s in response.context["segments"]], [self.second.pk])
        self.assertEqual([s.pk for s in response.context["excluded_segments"]], [self.first.pk])
        self.assertEqual(response.context["pending_count"], 0)
        self.assertContains(response, "Trechos excluídos (1)")
        self.client.post(self.save_url, {f"restore_segment_{self.first.pk}": "1"})
        self.first.refresh_from_db()
        self.job.refresh_from_db()
        self.assertIsNone(self.first.excluded_at)
        self.assertEqual(self.first.text, "REMOVE_THIS_EDIT")
        self.assertTrue(self.first.needs_review)
        self.assertTrue(self.job.needs_review)
        self.assertIn(b"REMOVE_THIS_EDIT", self.client.get(reverse("export", args=[self.job.pk, "txt"])).content)

    def test_delete_and_restore_same_segment_rejects_entire_save(self):
        response = self.client.post(self.save_url, {f"delete_segment_{self.first.pk}": "1", f"restore_segment_{self.first.pk}": "1", f"segment_{self.second.pk}": "must not save"})
        self.assertEqual(response.status_code, 400)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertIsNone(self.first.excluded_at)
        self.assertEqual(self.second.revised_text, "")

    def test_deleting_all_segments_produces_empty_srt_without_losing_rows(self):
        self.client.post(self.save_url, {f"delete_segment_{self.first.pk}": "1", f"delete_segment_{self.second.pk}": "1"})
        self.assertEqual(self.client.get(reverse("export", args=[self.job.pk, "srt"])).content, b"")
        self.assertEqual(self.job.segments.count(), 2)

    def test_other_user_cannot_exclude_or_restore(self):
        self.client.force_login(self.other)
        self.assertEqual(self.exclude().status_code, 404)
        self.assertEqual(self.client.post(self.save_url, {f"restore_segment_{self.first.pk}": "1"}).status_code, 404)
        self.first.refresh_from_db()
        self.assertIsNone(self.first.excluded_at)

    def test_archive_is_not_edited_or_reviewed_implicitly(self):
        self.exclude()
        self.first.refresh_from_db()
        when = self.first.excluded_at
        self.client.post(self.save_url, {"confirm_quality_review": "1", f"segment_{self.first.pk}": "unwanted change", f"delete_segment_{self.first.pk}": "1"})
        self.first.refresh_from_db()
        self.assertEqual(self.first.excluded_at, when)
        self.assertEqual(self.first.revised_text, "REMOVE_THIS_EDIT")
        self.assertEqual(self.first.review_reasons, ["english"])

    def test_pending_edits_are_saved_alongside_exclusion(self):
        self.client.post(self.save_url, {f"delete_segment_{self.first.pk}": "1", f"segment_{self.first.pk}": "Edited before deletion"})
        self.first.refresh_from_db()
        self.assertEqual(self.first.text, "Edited before deletion")
        self.assertIsNotNone(self.first.excluded_at)

    def test_excluded_gap_does_not_reappear_as_legacy_notice(self):
        self.first.start_ms, self.first.end_ms = 419, 2701
        self.first.review_reasons = ["missing_speech"]
        self.first.save()
        self.job.review_reasons = [{"code": "missing_speech", "start": .419159, "end": 2.70102}]
        self.job.save()
        self.exclude()
        self.job.refresh_from_db()
        self.assertFalse(self.job.needs_review)
        self.assertEqual(self.job.review_reasons, [])
