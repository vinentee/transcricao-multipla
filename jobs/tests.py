from decimal import Decimal
from unittest.mock import MagicMock, patch
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from django.urls import reverse
from pricing.models import PricingConfig
from pricing.services import quote_for_seconds
from payments.models import Payment
from .models import AudioJob, Quote, Speaker, TranscriptSegment, UsageRecord
from .tasks import transcribe_job, validate_job
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
    def test_failed_job_without_billing_is_repaired_before_retry(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.FAILED, original_key="retry.wav", original_filename="retry.wav", duration_seconds=60, failure_reason="falha")
        self.client.force_login(self.user)
        with patch("jobs.views.default_storage.exists", return_value=True), patch("jobs.tasks.transcribe_job.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/pedidos/{job.id}/tentar-novamente/")
        job.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(job.status, AudioJob.Status.PAID_QUEUED)
        self.assertEqual(job.failure_reason, "")
        self.assertEqual(job.quote.duration_seconds, 60)
        self.assertEqual(job.quote.total_brl, quote_for_seconds(60, self.config)["total_brl"])
        self.assertIsNotNone(job.quote.accepted_at)
        self.assertEqual(job.payment.status, Payment.Status.RECEIVED)
        self.assertEqual(job.payment.billing_type, "DEVELOPMENT")
        self.assertEqual(job.payment.amount_brl, job.quote.total_brl)
        delay.assert_called_once_with(str(job.id))

    @override_settings(DEBUG=True, DEV_BYPASS_PAYMENT=True)
    def test_retry_without_duration_is_blocked(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.FAILED, original_key="no-duration.wav", original_filename="no-duration.wav")
        self.client.force_login(self.user)
        with patch("jobs.views.default_storage.exists", return_value=True), patch("jobs.tasks.transcribe_job.delay") as delay:
            response = self.client.post(reverse("retry_transcription", args=[job.id]))
        job.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(job.status, AudioJob.Status.FAILED)
        self.assertFalse(Quote.objects.filter(job=job).exists())
        self.assertFalse(Payment.objects.filter(job=job).exists())
        delay.assert_not_called()

    @override_settings(DEBUG=True, DEV_BYPASS_PAYMENT=True)
    def test_retry_does_not_overwrite_an_incompatible_payment(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.FAILED, original_key="pending.wav", original_filename="pending.wav", duration_seconds=60)
        payment = Payment.objects.create(job=job, status=Payment.Status.PENDING, amount_brl="99.00")
        self.client.force_login(self.user)
        with patch("jobs.views.default_storage.exists", return_value=True), patch("jobs.tasks.transcribe_job.delay") as delay:
            response = self.client.post(reverse("retry_transcription", args=[job.id]))
        job.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(job.status, AudioJob.Status.FAILED)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.amount_brl, Decimal("99.00"))
        self.assertFalse(Quote.objects.filter(job=job).exists())
        delay.assert_not_called()

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


@override_settings(DEV_BYPASS_PAYMENT=True)
class JobTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="worker@example.com", password="senha-segura")

    def create_quote(self, job, total="10.00"):
        return Quote.objects.create(
            job=job,
            duration_seconds=job.duration_seconds or 60,
            total_brl=total,
            api_brl="1.0000",
            infra_brl="1.0000",
            pricing_snapshot={},
            expires_at=timezone.now() + timezone.timedelta(minutes=30),
        )

    def test_validation_rolls_back_duration_when_quote_creation_fails(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.VALIDATING, original_key="validation.wav", original_filename="validation.wav")
        with patch("jobs.tasks.inspect_audio", return_value=60), patch("jobs.tasks.create_quote", side_effect=ValueError("sem preço")):
            validate_job.run(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, AudioJob.Status.FAILED)
        self.assertIsNone(job.duration_seconds)
        self.assertFalse(Quote.objects.filter(job=job).exists())

    def test_worker_without_quote_fails_before_calling_openai(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.PAID_QUEUED, original_key="missing-quote.wav", original_filename="missing-quote.wav", duration_seconds=60)
        with patch("jobs.tasks.default_storage.exists", return_value=True), patch("jobs.tasks.OpenAITranscriptionProvider") as provider:
            transcribe_job.run(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, AudioJob.Status.FAILED)
        self.assertIn("cotação", job.failure_reason)
        provider.assert_not_called()

    def test_worker_without_received_payment_fails_before_calling_openai(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.PAID_QUEUED, original_key="missing-payment.wav", original_filename="missing-payment.wav", duration_seconds=60)
        self.create_quote(job)
        with patch("jobs.tasks.default_storage.exists", return_value=True), patch("jobs.tasks.OpenAITranscriptionProvider") as provider:
            transcribe_job.run(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, AudioJob.Status.FAILED)
        self.assertIn("pagamento", job.failure_reason)
        provider.assert_not_called()

    def test_worker_with_payment_amount_mismatch_fails_before_calling_openai(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.PAID_QUEUED, original_key="wrong-payment.wav", original_filename="wrong-payment.wav", duration_seconds=60)
        self.create_quote(job, total="10.00")
        Payment.objects.create(job=job, status=Payment.Status.RECEIVED, amount_brl="9.00")
        with patch("jobs.tasks.default_storage.exists", return_value=True), patch("jobs.tasks.OpenAITranscriptionProvider") as provider:
            transcribe_job.run(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, AudioJob.Status.FAILED)
        self.assertIn("diverge", job.failure_reason)
        provider.assert_not_called()

    def test_successful_worker_records_the_quoted_charge(self):
        job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.PAID_QUEUED, original_key="success.wav", original_filename="success.wav", duration_seconds=60)
        quote = self.create_quote(job, total="12.34")
        Payment.objects.create(job=job, status=Payment.Status.RECEIVED, amount_brl=quote.total_brl, billing_type="DEVELOPMENT")
        chunks = MagicMock()
        chunks.__enter__.return_value = [("/tmp/chunk.m4a", 0)]
        chunks.__exit__.return_value = False
        provider_result = {"segments": [{"speaker": "A", "start": 0, "end": 1, "text": "Teste"}], "usage": None}
        with patch("jobs.tasks.default_storage.exists", return_value=True), patch("jobs.tasks.prepared_transcription_chunks", return_value=chunks), patch("jobs.tasks.OpenAITranscriptionProvider") as provider:
            provider.return_value.transcribe.return_value = provider_result
            transcribe_job.run(str(job.id))
        job.refresh_from_db()
        usage = UsageRecord.objects.get(job=job)
        self.assertEqual(job.status, AudioJob.Status.READY)
        self.assertEqual(usage.charged_brl, Decimal("12.34"))
        provider.return_value.transcribe.assert_called_once_with("/tmp/chunk.m4a")
        with patch("jobs.tasks.OpenAITranscriptionProvider") as duplicate_provider:
            transcribe_job.run(str(job.id))
        duplicate_provider.assert_not_called()


@override_settings(DEV_BYPASS_PAYMENT=True)
class JobProgressTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="progress@example.com", password="senha")
        self.other = get_user_model().objects.create_user(email="private@example.com", password="senha")
        self.job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.VALIDATING, original_key="progress.wav", original_filename="progress.wav")
        self.private = AudioJob.objects.create(user=self.other, original_key="private.wav", original_filename="private.wav")

    def test_progress_requires_login_and_hides_other_users_jobs(self):
        for url in [reverse("jobs_status"), reverse("job_status", args=[self.job.id]), reverse("job_detail", args=[self.job.id]) + "?partial=1"]:
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("job_status", args=[self.private.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("job_detail", args=[self.private.id]) + "?partial=1").status_code, 404)
        payload = self.client.get(reverse("jobs_status")).json()
        self.assertEqual([job["id"] for job in payload["jobs"]], [str(self.job.id)])

    def test_status_and_fragment_follow_validation_quote_and_result(self):
        self.client.force_login(self.user)
        url = reverse("job_detail", args=[self.job.id]) + "?partial=1"
        status_url = reverse("job_status", args=[self.job.id])
        initial = self.client.get(status_url).json()
        self.assertTrue(initial["busy"])
        self.assertEqual(initial["label"], "Validando")
        response = self.client.get(url)
        self.assertContains(response, "Validando seu áudio")
        self.assertNotContains(response, "<!doctype")
        self.assertIn("no-store", response.headers["Cache-Control"])
        Quote.objects.create(job=self.job, duration_seconds=60, total_brl="10.00", api_brl="1", infra_brl="1", pricing_snapshot={}, expires_at=timezone.now() + timezone.timedelta(minutes=30))
        self.job.transition(AudioJob.Status.QUOTED)
        self.assertContains(self.client.get(url), "Confirmar pagamento de teste")
        quoted = self.client.get(status_url).json()
        self.assertFalse(quoted["busy"])
        self.assertNotEqual(quoted["version"], initial["version"])
        self.job.status = AudioJob.Status.READY
        self.job.save()
        response = self.client.get(url)
        self.assertContains(response, "Revise sua transcrição")
        self.assertContains(response, "PDF")
        self.assertNotContains(response, "processing-card")

    @override_settings(MAX_UPLOAD_BYTES=104857600)
    def test_dashboard_displays_configured_upload_limit(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("dashboard")), "100 MB")

    def test_deleted_jobs_are_excluded_from_progress(self):
        self.client.force_login(self.user)
        self.job.audio_deleted_at = timezone.now()
        self.job.save()
        self.assertEqual(self.client.get(reverse("jobs_status")).json(), {"jobs": []})
        self.assertEqual(self.client.get(reverse("job_status", args=[self.job.id])).status_code, 404)
