import logging

from celery import shared_task
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from .models import AudioJob, UsageRecord
from .pipeline import transcribe_with_quality
from .media import build_playback, delete_media
from .costs import estimate_cost
from .services import create_quote, inspect_audio, save_provider_segments

logger = logging.getLogger(__name__)

@shared_task(bind=True, autoretry_for=(OSError, TimeoutError), retry_backoff=True, retry_kwargs={"max_retries": 3})
def validate_job(self, job_id):
    job = AudioJob.objects.get(pk=job_id)
    if job.status != AudioJob.Status.VALIDATING:
        return
    try:
        duration_seconds = inspect_audio(job)
        with transaction.atomic():
            job = AudioJob.objects.select_for_update().get(pk=job_id)
            if job.status != AudioJob.Status.VALIDATING:
                return
            job.duration_seconds = duration_seconds
            job.save(update_fields=["duration_seconds"])
            create_quote(job)
            job.transition(AudioJob.Status.QUOTED)
        logger.info("Audio validation and quote completed for job %s", job_id)
    except Exception as exc:
        with transaction.atomic():
            job = AudioJob.objects.select_for_update().get(pk=job_id)
            if job.status == AudioJob.Status.VALIDATING:
                job.transition(AudioJob.Status.FAILED, str(exc)[:300])
        logger.exception("Audio validation failed for job %s", job_id)

@shared_task(bind=True, autoretry_for=(OSError, TimeoutError), retry_backoff=True, retry_kwargs={"max_retries": 3})
def transcribe_job(self, job_id):
    with transaction.atomic():
        job = AudioJob.objects.select_for_update().get(pk=job_id)
        if job.status != AudioJob.Status.PAID_QUEUED:
            return
        if job.audio_deleted_at or not default_storage.exists(job.original_key):
            job.transition(AudioJob.Status.FAILED, "O áudio original não está disponível para transcrição.")
            logger.error("Transcription blocked because audio is unavailable for job %s", job_id)
            return
        try:
            quote = job.quote
        except AudioJob.quote.RelatedObjectDoesNotExist:
            job.transition(AudioJob.Status.FAILED, "O pedido não possui uma cotação válida.")
            logger.error("Transcription blocked because quote is missing for job %s", job_id)
            return
        try:
            payment = job.payment
        except AudioJob.payment.RelatedObjectDoesNotExist:
            job.transition(AudioJob.Status.FAILED, "O pedido não possui um pagamento confirmado.")
            logger.error("Transcription blocked because payment is missing for job %s", job_id)
            return
        from payments.models import Payment
        if payment.status != Payment.Status.RECEIVED or payment.amount_brl != quote.total_brl:
            job.transition(AudioJob.Status.FAILED, "O pagamento do pedido não está confirmado ou diverge da cotação.")
            logger.error("Transcription blocked because payment is invalid for job %s", job_id)
            return
        charged_brl = quote.total_brl
        job.transition(AudioJob.Status.PROCESSING)
    logger.info("Transcription started for job %s", job_id)
    try:
        result = transcribe_with_quality(job)
        input_tokens = sum(item["input_tokens"] for item in result["usage"])
        output_tokens = sum(item["output_tokens"] for item in result["usage"])
        with transaction.atomic():
            job = AudioJob.objects.select_for_update().get(pk=job_id)
            if job.audio_deleted_at:
                if job.can_transition(AudioJob.Status.CANCELLED): job.transition(AudioJob.Status.CANCELLED)
                return
            job.review_reasons = result["issues"]
            job.needs_review = bool(result["issues"])
            job.quality_metadata = result["metadata"]
            save_provider_segments(job, result["segments"], group_turns=True)
            UsageRecord.objects.update_or_create(job=job, defaults={
                "input_tokens": input_tokens, "output_tokens": output_tokens, "charged_brl": charged_brl,
                "provider_usage": result["usage"], "actual_cost_brl": estimate_cost(quote.pricing_snapshot, result["usage"]),
            })
            job.transition(AudioJob.Status.READY)
        logger.info("Transcription completed for job %s", job_id)
    except Exception as exc:
        job = AudioJob.objects.get(pk=job_id)
        if settings.DEV_BYPASS_PAYMENT:
            job.transition(AudioJob.Status.FAILED, str(exc)[:300])
        else:
            from payments.tasks import request_refund
            job.transition(AudioJob.Status.REFUND_PENDING, str(exc)[:300])
            request_refund.delay(str(job.id))
        logger.exception("Transcription failed for job %s", job_id)
        raise

@shared_task
def prepare_playback_job(job_id):
    job = AudioJob.objects.get(pk=job_id)
    if not job.audio_deleted_at:
        build_playback(job)


@shared_task
def cleanup_expired_audio():
    ids = AudioJob.objects.filter(audio_deleted_at__isnull=True, delete_audio_after__lte=timezone.now()).values_list("pk", flat=True)
    for job_id in ids:
        with transaction.atomic():
            job = AudioJob.objects.select_for_update().get(pk=job_id)
            if job.audio_deleted_at:
                continue
            if delete_media(job):
                job.audio_deleted_at = timezone.now()
                job.playback_key, job.playback_status = "", ""
                job.save(update_fields=["audio_deleted_at", "playback_key", "playback_status", "updated_at"])
