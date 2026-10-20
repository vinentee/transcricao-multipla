from celery import shared_task
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from .models import AudioJob, UsageRecord
from .providers import OpenAITranscriptionProvider
from .services import create_quote, inspect_audio, prepared_transcription_chunks, save_provider_segments

@shared_task(bind=True, autoretry_for=(OSError, TimeoutError), retry_backoff=True, retry_kwargs={"max_retries": 3})
def validate_job(self, job_id):
    job = AudioJob.objects.get(pk=job_id)
    if job.status != AudioJob.Status.VALIDATING:
        return
    try:
        job.duration_seconds = inspect_audio(job)
        job.save(update_fields=["duration_seconds"])
        create_quote(job)
        job.transition(AudioJob.Status.QUOTED)
    except Exception as exc:
        job.transition(AudioJob.Status.FAILED, str(exc)[:300])

@shared_task(bind=True, autoretry_for=(OSError, TimeoutError), retry_backoff=True, retry_kwargs={"max_retries": 3})
def transcribe_job(self, job_id):
    with transaction.atomic():
        job = AudioJob.objects.select_for_update().get(pk=job_id)
        if job.status != AudioJob.Status.PAID_QUEUED:
            return
        job.transition(AudioJob.Status.PROCESSING)
    try:
        all_segments, last_end_ms, input_tokens, output_tokens = [], 0, 0, 0
        with prepared_transcription_chunks(job) as chunks:
            for chunk_path, offset_seconds in chunks:
                result = OpenAITranscriptionProvider().transcribe(chunk_path)
                for segment in result["segments"]:
                    item = dict(segment)
                    item["start"], item["end"] = float(item["start"]) + offset_seconds, float(item["end"]) + offset_seconds
                    end_ms = int(item["end"] * 1000)
                    if end_ms <= last_end_ms:
                        continue
                    if int(item["start"] * 1000) < last_end_ms:
                        item["start"] = last_end_ms / 1000
                    last_end_ms = end_ms
                    all_segments.append(item)
                usage = result.get("usage") or {}
                input_tokens += getattr(usage, "input_tokens", 0) or 0
                output_tokens += getattr(usage, "output_tokens", 0) or 0
        with transaction.atomic():
            job = AudioJob.objects.select_for_update().get(pk=job_id)
            if job.audio_deleted_at:
                if job.can_transition(AudioJob.Status.CANCELLED): job.transition(AudioJob.Status.CANCELLED)
                return
            save_provider_segments(job, all_segments)
            UsageRecord.objects.update_or_create(job=job, defaults={"input_tokens": input_tokens, "output_tokens": output_tokens, "charged_brl": job.quote.total_brl})
            job.transition(AudioJob.Status.READY)
    except Exception as exc:
        job = AudioJob.objects.get(pk=job_id)
        if settings.DEV_BYPASS_PAYMENT:
            job.transition(AudioJob.Status.FAILED, str(exc)[:300])
        else:
            from payments.tasks import request_refund
            job.transition(AudioJob.Status.REFUND_PENDING, str(exc)[:300])
            request_refund.delay(str(job.id))
        raise

@shared_task
def cleanup_expired_audio():
    for job in AudioJob.objects.filter(audio_deleted_at__isnull=True, delete_audio_after__lte=timezone.now()):
        default_storage.delete(job.original_key)
        if not default_storage.exists(job.original_key):
            job.audio_deleted_at = timezone.now()
            job.save(update_fields=["audio_deleted_at"])
