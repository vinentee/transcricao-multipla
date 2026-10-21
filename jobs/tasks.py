import logging

from celery import shared_task
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from .models import AudioJob, UsageRecord
from .audio import AudioChunk, SpeakerReferences
from .quality import assess, choose_retry, merge_chunks
from .providers import OpenAITranscriptionProvider
from .services import create_quote, inspect_audio, prepared_transcription_chunks, save_provider_segments

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
        chunk_results, input_tokens, output_tokens, usage_seconds = [], 0, 0, 0
        references, review_issues, retries = SpeakerReferences(), [], 0
        provider = OpenAITranscriptionProvider()

        def request(chunk):
            nonlocal input_tokens, output_tokens, usage_seconds
            payload = references.payload()
            result = provider.transcribe(chunk.path, known_speakers=payload) if payload else provider.transcribe(chunk.path)
            usage = result.get("usage") or {}
            if hasattr(usage, "model_dump"):
                usage = usage.model_dump()
            elif not isinstance(usage, dict):
                usage = vars(usage)
            input_tokens += usage.get("input_tokens", 0) or 0
            output_tokens += usage.get("output_tokens", 0) or 0
            usage_seconds += usage.get("seconds", 0) or 0
            return assess(result["segments"], chunk.duration, chunk.speech, settings.TRANSCRIPTION_LANGUAGE)

        with prepared_transcription_chunks(job) as chunks, references:
            for entry in chunks:
                # Compatibility with older internal adapters/tests returning tuples.
                chunk = entry if isinstance(entry, AudioChunk) else AudioChunk(entry[0], entry[1], min(settings.TRANSCRIPTION_CHUNK_SECONDS, (job.duration_seconds or settings.TRANSCRIPTION_CHUNK_SECONDS) - entry[1]))
                original = request(chunk)
                chosen = original
                extra_issues = []
                if original[1]:
                    retries += 1
                    try:
                        chosen = choose_retry(original, request(chunk))
                        if chosen[1]:
                            extra_issues.append({"code": "retry_unresolved", "start": 0, "end": chunk.duration})
                    except Exception:
                        # Optional verification must not destroy a usable first result.
                        extra_issues.append({"code": "retry_failed", "start": 0, "end": chunk.duration})
                        logger.warning("Quality retry failed for job %s at offset %.3f", job_id, chunk.offset)
                segments, issues, _ = chosen
                if extra_issues:
                    for item in segments:
                        item["review_reasons"] = sorted(set(item["review_reasons"]) | {i["code"] for i in extra_issues})
                references.identify(segments, chunk)
                chunk_results.append((chunk.offset, chunk.duration, segments))
                review_issues.extend({**issue, "start": issue["start"] + chunk.offset, "end": issue["end"] + chunk.offset} for issue in issues + extra_issues)
                logger.info("Quality check job %s offset %.3f issues=%s retries=%s", job_id, chunk.offset, sorted({i["code"] for i in issues + extra_issues}), retries)
            all_segments = merge_chunks(chunk_results)
            for item in all_segments:
                for code in item["review_reasons"]:
                    issue = {"code": code, "start": item["start"], "end": item["end"]}
                    if issue not in review_issues:
                        review_issues.append(issue)
        with transaction.atomic():
            job = AudioJob.objects.select_for_update().get(pk=job_id)
            if job.audio_deleted_at:
                if job.can_transition(AudioJob.Status.CANCELLED): job.transition(AudioJob.Status.CANCELLED)
                return
            job.review_reasons = review_issues
            job.needs_review = bool(review_issues)
            job.quality_metadata = {"version": 1, "chunks": len(chunk_results), "retries": retries, "language": settings.TRANSCRIPTION_LANGUAGE, "provider_audio_seconds": usage_seconds}
            save_provider_segments(job, all_segments)
            UsageRecord.objects.update_or_create(job=job, defaults={"input_tokens": input_tokens, "output_tokens": output_tokens, "charged_brl": charged_brl})
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
def cleanup_expired_audio():
    for job in AudioJob.objects.filter(audio_deleted_at__isnull=True, delete_audio_after__lte=timezone.now()):
        default_storage.delete(job.original_key)
        if not default_storage.exists(job.original_key):
            job.audio_deleted_at = timezone.now()
            job.save(update_fields=["audio_deleted_at"])
