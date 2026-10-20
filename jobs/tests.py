from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from django.urls import reverse
from pricing.models import PricingConfig
from pricing.services import quote_for_seconds
from .models import AudioJob, Speaker, TranscriptSegment
from .views import timestamp
from .forms import UploadForm
from .services import ALLOWED_EXTENSIONS


class UploadFormTests(TestCase):
    @override_settings(MAX_UPLOAD_BYTES=5)
    def test_file_above_configured_limit_is_rejected(self):
        form = UploadForm(files={"file": SimpleUploadedFile("large.mp3", b"123456")})

        self.assertFalse(form.is_valid())
        self.assertIn("O limite é", form.errors["file"][0])

class PricingAndExportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="ana@example.com", password="senha-segura")
        self.config = PricingConfig.objects.create(active=True, validated_at=timezone.now(), usd_brl="5", audio_tokens_per_minute="100", output_tokens_per_minute="10", input_usd_per_million="2.5", output_usd_per_million="10", infra_brl_per_minute="0.01", gateway_fixed_brl="0", gateway_percent="0", margin="0.10")
    def test_price_rounds_up_to_cent(self):
        quote = quote_for_seconds(60, self.config)
        self.assertEqual(quote["total_brl"], Decimal("0.02"))
    def test_opus_is_an_accepted_container(self):
        self.assertIn("opus", ALLOWED_EXTENSIONS)
    def test_transition_rejects_skip(self):
        job = AudioJob.objects.create(user=self.user, original_key="x", original_filename="x.wav")
        with self.assertRaises(ValueError): job.transition(AudioJob.Status.READY)
    def test_srt_export_requires_owner_and_uses_valid_timestamp(self):
        job=AudioJob.objects.create(user=self.user, status=AudioJob.Status.READY, original_key="x", original_filename="x.wav")
        speaker=Speaker.objects.create(job=job, number=1, display_name="Ana")
        TranscriptSegment.objects.create(job=job, speaker=speaker, ordinal=1, start_ms=0, end_ms=1250, raw_text="Olá")
        self.client.force_login(self.user)
        response=self.client.get(f"/pedidos/{job.id}/exportar/srt/")
        self.assertContains(response, "00:00:00,000 --> 00:00:01,250")
        self.assertEqual(timestamp(3661001), "01:01:01,001")

    def test_pdf_export_contains_revised_transcript_and_metadata(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.READY, original_key="pdf.wav", original_filename="reunião.wav", duration_seconds=125)
        speaker = Speaker.objects.create(job=job, number=1, display_name="Ana")
        TranscriptSegment.objects.create(job=job, speaker=speaker, ordinal=1, start_ms=0, end_ms=1250, raw_text="Rascunho", revised_text="Texto revisado")
        self.client.force_login(self.user)
        response = self.client.get(reverse("export", args=[job.id, "pdf"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn(b"Texto revisado", response.content)
        self.assertIn(b"Ana", response.content)

    def test_pdf_export_paginates_a_long_transcript(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.READY, original_key="long.pdf.wav", original_filename="longa.wav")
        speaker = Speaker.objects.create(job=job, number=1, display_name="Ana")
        TranscriptSegment.objects.bulk_create([
            TranscriptSegment(job=job, speaker=speaker, ordinal=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, raw_text="Texto de transcrição para conferir paginação automática.")
            for i in range(100)
        ])
        self.client.force_login(self.user)
        response = self.client.get(reverse("export", args=[job.id, "pdf"]))
        self.assertGreaterEqual(response.content.count(b"/Type /Page"), 2)

    def test_export_is_hidden_for_another_user_or_deleted_audio(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.READY, original_key="private.wav", original_filename="private.wav")
        other = get_user_model().objects.create_user(email="other@example.com", password="senha-segura")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("export", args=[job.id, "pdf"])).status_code, 404)
        job.audio_deleted_at = timezone.now()
        job.save(update_fields=["audio_deleted_at"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("export", args=[job.id, "pdf"])).status_code, 404)

    @override_settings(DEBUG=True, DEV_BYPASS_PAYMENT=True)
    def test_failed_job_can_be_retried_without_a_new_payment(self):
        from unittest.mock import patch
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.FAILED, original_key="retry.wav", original_filename="retry.wav", failure_reason="falha")
        from django.core.files.storage import default_storage
        default_storage.save(job.original_key, __import__("django").core.files.base.ContentFile(b"audio"))
        self.client.force_login(self.user)
        with patch("jobs.tasks.transcribe_job.delay"):
            response = self.client.post(f"/pedidos/{job.id}/tentar-novamente/")
        job.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(job.status, AudioJob.Status.PAID_QUEUED)
        self.assertEqual(job.failure_reason, "")

    def test_delete_marks_audio_only_after_storage_removal(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.QUOTED, original_key="delete.wav", original_filename="delete.wav")
        default_storage.save(job.original_key, ContentFile(b"audio"))
        self.client.force_login(self.user)
        response = self.client.post(f"/pedidos/{job.id}/audio/excluir/")
        job.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(job.audio_deleted_at)
        self.assertFalse(default_storage.exists(job.original_key))
        self.assertEqual(job.status, AudioJob.Status.CANCELLED)
        self.assertRedirects(response, reverse("dashboard"))
        self.assertNotContains(self.client.get(reverse("dashboard")), job.original_filename)
        self.assertEqual(self.client.get(reverse("job_detail", args=[job.id])).status_code, 404)
