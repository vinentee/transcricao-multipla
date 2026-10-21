import importlib
import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pricing.models import PricingConfig
from pricing.services import quote_for_seconds
from .audio import PreparedAudio
from .costs import estimate_cost
from .media import build_playback
from .models import AudioJob, Speaker, TranscriptSegment
from .pipeline import verify_segments
from .providers import DeepgramTranscriptionProvider, OpenAIVerificationProvider, ProviderError
from .services import create_quote
from .tasks import cleanup_expired_audio
from .test_quality import segment, write_wave


@override_settings(OPENAI_API_KEY="test-key")
class VerificationBudgetTests(SimpleTestCase):
    def test_budget_is_chronological_one_attempt_per_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.wav")
            write_wave(source, 100)
            segments = [dict(segment("Thank you.", start, start + 2, "0"), review_reasons=["english"]) for start in reversed(range(0, 24, 2))]
            with patch("jobs.pipeline.OpenAIVerificationProvider") as verifier:
                verifier.return_value.transcribe.return_value = {"text": "Obrigado."}
                stats, usage = verify_segments(segments, PreparedAudio(source, 100, []), "gpt-transcribe")
            self.assertEqual(stats["attempts"], 10)
            self.assertEqual(stats["seconds"], 20)
            self.assertEqual(len(usage), 10)
            by_time = sorted(segments, key=lambda s: s["start"])
            self.assertTrue(all(s.get("corrected_text") == "Obrigado." for s in by_time[:10]))
            self.assertTrue(all("verification_budget" in s["review_reasons"] for s in by_time[10:]))

    def test_english_candidate_and_changed_numbers_are_not_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.wav")
            write_wave(source, 100)
            segments = [dict(segment("Thank you 500.", 0, 2), review_reasons=["english"])]
            with patch("jobs.pipeline.OpenAIVerificationProvider") as verifier:
                verifier.return_value.transcribe.return_value = {"text": "Muito obrigado por participar desta conversa sobre o valor de 600 reais."}
                verify_segments(segments, PreparedAudio(source, 100, []), "gpt-transcribe")
            self.assertFalse(segments[0]["alternatives"][0]["applied"])
            self.assertEqual(segments[0]["text"], "Thank you 500.")

    def test_short_english_is_flagged_but_names_are_not(self):
        from .quality import assess
        self.assertIn("english", assess([segment("Thank you.")], 3, [])[0][0]["review_reasons"])
        self.assertNotIn("english", assess([segment("O John explicou o feedback.")], 3, [])[0][0]["review_reasons"])


@override_settings(DEEPGRAM_API_KEY="not-a-real-key")
class ProviderAdapterTests(SimpleTestCase):
    def test_whole_file_parameters_and_speaker_zero(self):
        payload = {"metadata": {"duration": 3601}, "results": {"utterances": [
            {"start": .5, "end": 2, "speaker": 0, "transcript": "Bom dia.", "words": [{"word": "bom", "start": .5, "end": 1, "speaker": 0}]},
            {"start": 2, "end": 3, "speaker": 1, "transcript": "Olá."},
        ]}}
        with tempfile.NamedTemporaryFile() as source, patch("jobs.providers.requests.post") as post:
            post.return_value.json.return_value = payload
            result = DeepgramTranscriptionProvider("nova-3", "pt-BR").transcribe(source.name)
            params = post.call_args.kwargs["params"]
            self.assertEqual(params, {"model": "nova-3", "language": "pt-BR", "diarize_model": "v2", "utterances": "true", "punctuate": "true"})
            self.assertEqual(post.call_count, 1)
            self.assertEqual(result["segments"][0]["speaker"], "0")
            self.assertEqual(result["segments"][1]["speaker"], "1")
            self.assertEqual(result["usage"]["seconds"], 3601)

    def test_words_without_utterances_preserve_speaker_changes(self):
        payload = {"metadata": {"duration": 5}, "results": {"channels": [{"alternatives": [{"words": [
            {"word": "bom", "start": 0, "end": 1, "speaker": 0},
            {"word": "dia", "start": 1, "end": 2, "speaker": 0},
            {"word": "olá", "start": 2, "end": 3, "speaker": 1},
        ]}]}]}}
        with tempfile.NamedTemporaryFile() as source, patch("jobs.providers.requests.post") as post:
            post.return_value.json.return_value = payload
            result = DeepgramTranscriptionProvider().transcribe(source.name)
        self.assertEqual([s["text"] for s in result["segments"]], ["bom dia", "olá"])
        self.assertEqual([s["speaker"] for s in result["segments"]], ["0", "1"])

    def test_remote_error_is_sanitized(self):
        with tempfile.NamedTemporaryFile() as source, patch("jobs.providers.requests.post", side_effect=requests.HTTPError("SECRET transcript and API key")):
            with self.assertRaises(ProviderError) as caught:
                DeepgramTranscriptionProvider().transcribe(source.name)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_verifier_uses_new_model_languages_and_literal_context(self):
        with tempfile.NamedTemporaryFile() as source, patch("openai.OpenAI") as client:
            client.return_value.audio.transcriptions.create.return_value = SimpleNamespace(text="Obrigado.", usage={})
            OpenAIVerificationProvider("gpt-transcribe").transcribe(source.name)
            kwargs = client.return_value.audio.transcriptions.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "gpt-transcribe")
            self.assertEqual(kwargs["extra_body"], {"languages": ["pt"]})
            self.assertNotIn("language", kwargs)
            self.assertIn("sem tradução", kwargs["prompt"])


@override_settings(S3_ENABLED=False)
class PlaybackTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = FileSystemStorage(location=self.directory.name)
        self.patches = [patch("jobs.media.default_storage", self.storage), patch("jobs.views.default_storage", self.storage)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.user = get_user_model().objects.create_user(email="listen@example.com", password="test-password")
        self.other = get_user_model().objects.create_user(email="other@example.com", password="test-password")
        self.storage.save("source.wav", ContentFile(b"original"))
        self.storage.save("review.mp3", ContentFile(b"0123456789"))
        self.job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.READY, original_key="source.wav", original_filename="source.wav", duration_seconds=10,
                                          playback_key="review.mp3", playback_status="ready")
        self.client.force_login(self.user)
        self.url = reverse("playback_audio", args=[self.job.pk])

    def test_full_head_single_and_suffix_ranges(self):
        response = self.client.get(self.url)
        self.assertEqual(b"".join(response.streaming_content), b"0123456789")
        response = self.client.get(self.url, HTTP_RANGE="bytes=2-5")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 2-5/10")
        self.assertEqual(b"".join(response.streaming_content), b"2345")
        response = self.client.get(self.url, HTTP_RANGE="bytes=-3")
        self.assertEqual(b"".join(response.streaming_content), b"789")
        response = self.client.head(self.url, HTTP_RANGE="bytes=0-1")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Length"], "2")
        self.assertEqual(response.content, b"")
        for value in ("bytes=100-", "bytes=3-1", "bytes=0-1,3-4", "bytes=-0"):
            self.assertEqual(self.client.get(self.url, HTTP_RANGE=value).status_code, 416)

    def test_other_user_and_deleted_audio_are_denied(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.user)
        self.job.audio_deleted_at = timezone.now()
        self.job.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(reverse("request_playback", args=[self.job.pk])).status_code, 404)

    def test_signed_r2_url_has_short_expiry(self):
        with override_settings(S3_ENABLED=True), patch("jobs.media.default_storage") as storage:
            storage.url.return_value = "https://private.example/audio?signature=temporary"
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 302)
            storage.url.assert_called_once_with("review.mp3", parameters={"ResponseContentType": "audio/mpeg"}, expire=300)

    def test_original_and_playback_are_deleted_together(self):
        response = self.client.post(reverse("delete_audio", args=[self.job.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.storage.exists("source.wav"))
        self.assertFalse(self.storage.exists("review.mp3"))
        self.job.refresh_from_db()
        self.assertIsNotNone(self.job.audio_deleted_at)

    def test_expiration_cleans_both_files(self):
        self.job.delete_audio_after = timezone.now() - timezone.timedelta(seconds=1)
        self.job.save()
        cleanup_expired_audio.run()
        self.assertFalse(self.storage.exists("review.mp3"))
        self.assertFalse(self.storage.exists("source.wav"))

    def test_preparation_is_enqueued_once_and_keeps_review(self):
        self.job.playback_status, self.job.playback_key = "", ""
        self.job.save()
        saved = TranscriptSegment.objects.create(job=self.job, ordinal=1, start_ms=0, end_ms=1000, raw_text="original", revised_text="edição humana")
        with patch("jobs.tasks.prepare_playback_job.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("request_playback", args=[self.job.pk]))
                self.client.post(reverse("request_playback", args=[self.job.pk]))
            delay.assert_called_once_with(str(self.job.pk))
        saved.refresh_from_db()
        self.assertEqual(saved.revised_text, "edição humana")

    def test_real_mp3_generation_and_delete_race(self):
        path = os.path.join(self.directory.name, "normalized.wav")
        write_wave(path, 3)
        self.job.playback_status, self.job.playback_key = "", ""
        self.job.save()
        key = build_playback(self.job, path)
        self.assertTrue(self.storage.exists(key))
        self.job.refresh_from_db()
        self.assertEqual(self.job.playback_status, "ready")
        self.job.playback_status, self.job.playback_key = "", ""
        self.job.save()
        real_save = self.storage.save
        def delete_during_save(*args, **kwargs):
            generated = real_save(*args, **kwargs)
            AudioJob.objects.filter(pk=self.job.pk).update(audio_deleted_at=timezone.now())
            return generated
        with patch.object(self.storage, "save", side_effect=delete_during_save):
            self.assertIsNone(build_playback(self.job, path))
        self.job.refresh_from_db()
        self.assertEqual(self.job.playback_key, "")


class ProviderPricingTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email="quote@example.com", password="test-password")
        self.job = AudioJob.objects.create(user=user, original_key="quote.wav", original_filename="quote.wav", duration_seconds=60)
        self.config = PricingConfig.objects.create(provider="deepgram", active=True, validated_at=timezone.now(), api_usd_per_minute="0.01", verification_usd_per_minute="0.02", usd_brl="5", infra_brl_per_minute="0", gateway_fixed_brl="0", gateway_percent="0", margin="0")

    @override_settings(TRANSCRIPTION_PROVIDER="deepgram", DEEPGRAM_API_KEY="", OPENAI_API_KEY="test")
    def test_missing_main_key_blocks_quote_before_charge(self):
        with self.assertRaisesRegex(ValueError, "chave"):
            create_quote(self.job)
        self.assertFalse(hasattr(self.job, "quote"))

    @override_settings(TRANSCRIPTION_PROVIDER="deepgram", DEEPGRAM_API_KEY="test", DEEPGRAM_MODEL="nova-3")
    def test_quote_snapshot_retains_provider_model_and_price(self):
        quote = create_quote(self.job)
        self.assertEqual(str(quote.total_brl), "0.07")
        self.assertEqual(quote.pricing_snapshot["provider"], "deepgram")
        with override_settings(TRANSCRIPTION_PROVIDER="openai", DEEPGRAM_MODEL="changed"):
            from .providers import freeze_provider
            freeze_provider(self.job)
        self.assertEqual(self.job.transcription_provider, "deepgram")
        self.assertEqual(self.job.transcription_model, "nova-3")
        quote.refresh_from_db()
        self.assertEqual(str(quote.total_brl), "0.07")

    def test_duration_cost_uses_main_and_verification_tariffs(self):
        from decimal import Decimal
        result = quote_for_seconds(60, self.config)
        self.assertEqual(result["api_brl"], Decimal("0.0700"))
        snapshot = {"api_usd_per_minute": ".01", "verification_usd_per_minute": ".02", "usd_brl": "5"}
        usages = [{"provider": "deepgram", "role": "primary", "audio_seconds": 60}, {"provider": "openai", "role": "verification", "audio_seconds": 6}]
        self.assertEqual(estimate_cost(snapshot, usages), Decimal(".0600"))
        self.assertIsNone(estimate_cost({}, usages))

    def test_manual_empty_text_is_preserved(self):
        saved = TranscriptSegment.objects.create(job=self.job, ordinal=1, start_ms=0, end_ms=1000, raw_text="fala inventada")
        self.job.status = AudioJob.Status.READY
        self.job.save()
        self.client.force_login(self.job.user)
        self.client.post(reverse("save_review", args=[self.job.pk]), {f"segment_{saved.pk}": ""})
        saved.refresh_from_db()
        self.assertEqual(saved.text, "")
        self.assertTrue(saved.has_manual_revision)
        self.assertEqual(saved.raw_text, "fala inventada")

    def test_new_manual_speaker_can_be_created_and_assigned(self):
        saved = TranscriptSegment.objects.create(job=self.job, ordinal=1, start_ms=0, end_ms=1000, raw_text="Olá", needs_review=True, review_reasons=["unknown_speaker"])
        self.job.status = AudioJob.Status.READY
        self.job.save()
        self.client.force_login(self.job.user)
        response = self.client.post(reverse("save_review", args=[self.job.pk]), {"new_speaker_new_1": "Ana", f"assign_{saved.pk}": "new_1"})
        self.assertEqual(response.status_code, 302)
        saved.refresh_from_db()
        self.assertEqual(saved.speaker.display_name, "Ana")
        self.assertFalse(saved.needs_review)

    def test_long_editor_fetches_speakers_once(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        speaker = Speaker.objects.create(job=self.job, number=1, display_name="Ana")
        self.job.status = AudioJob.Status.READY
        self.job.save()
        TranscriptSegment.objects.bulk_create([
            TranscriptSegment(job=self.job, speaker=speaker, ordinal=i + 1, start_ms=i * 1000, end_ms=(i + 1) * 1000, raw_text="Olá")
            for i in range(80)
        ])
        self.client.force_login(self.job.user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("job_detail", args=[self.job.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 7)

    def test_legacy_data_backfill_keeps_human_review(self):
        speaker = Speaker.objects.create(job=self.job, number=1, display_name="Ana", provider_label="A")
        saved = TranscriptSegment.objects.create(job=self.job, speaker=speaker, ordinal=1, start_ms=0, end_ms=1000, raw_text="rascunho", revised_text="texto humano")
        migration = importlib.import_module("jobs.migrations.0003_audiojob_playback_key_audiojob_playback_requested_at_and_more")
        migration.preserve_legacy_jobs(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        self.job.refresh_from_db()
        saved.refresh_from_db()
        self.assertEqual(self.job.transcription_provider, "openai")
        self.assertEqual(saved.revised_text, "texto humano")
        self.assertEqual(saved.provider_label, "A")

    @override_settings(DEEPGRAM_API_KEY="test", OPENAI_API_KEY="test")
    def test_comparison_report_is_private_separate_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.wav", Path(directory) / "report.json"
            write_wave(str(source), 2)
            with patch("jobs.management.commands.compare_transcription.transcribe_prepared", return_value={"segments": [], "usage": []}) as transcribe:
                call_command("compare_transcription", audio=str(source), output=str(output), stdout=StringIO())
            self.assertEqual(transcribe.call_count, 2)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(json.loads(output.read_text())["results"]), {"deepgram", "openai"})
            self.assertEqual(self.job.segments.count(), 0)
            with self.assertRaises(CommandError):
                call_command("compare_transcription", audio=str(source), output=str(output), stdout=StringIO())
