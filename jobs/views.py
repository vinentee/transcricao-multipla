import logging
import uuid
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from .forms import UploadForm
from .models import AudioJob
from .services import create_quote
from .tasks import validate_job

logger = logging.getLogger(__name__)


class RetryPreparationError(Exception):
    pass

def owned_job(request, pk):
    return get_object_or_404(AudioJob, pk=pk, user=request.user)

@login_required
def dashboard(request):
    jobs = request.user.audio_jobs.filter(audio_deleted_at__isnull=True)
    return render(request, "jobs/dashboard.html", {"jobs": jobs, "form": UploadForm(), "max_upload_mb": settings.MAX_UPLOAD_BYTES / (1024 * 1024)})


WATCHED_STATUSES = {AudioJob.Status.UPLOADING, AudioJob.Status.VALIDATING, AudioJob.Status.AWAITING_PAYMENT, AudioJob.Status.PAID_QUEUED, AudioJob.Status.PROCESSING}


def status_payload(job):
    return {
        "id": str(job.id), "status": job.status, "label": job.get_status_display(),
        "busy": job.status in WATCHED_STATUSES, "version": job.updated_at.isoformat(),
        "detail_url": reverse("job_detail", args=[job.id]),
    }


@login_required
@never_cache
def jobs_status(request):
    jobs = request.user.audio_jobs.filter(audio_deleted_at__isnull=True)
    return JsonResponse({"jobs": [status_payload(job) for job in jobs]})

@login_required
@require_POST
def upload(request):
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Upload inválido.")
        return redirect("dashboard")
    f = form.cleaned_data["file"]
    key = f"audio/{request.user.id}/{uuid.uuid4()}/{f.name}"
    default_storage.save(key, f)
    job = AudioJob.objects.create(user=request.user, original_key=key, original_filename=f.name, content_type=f.content_type or "", file_size=f.size)
    job.transition(AudioJob.Status.VALIDATING)
    validate_job.delay(str(job.id))
    messages.success(request, "Arquivo enviado; a validação e cotação serão feitas em seguida.")
    return redirect("job_detail", pk=job.id)

@login_required
@never_cache
def job_detail(request, pk):
    job = owned_job(request, pk)
    if job.audio_deleted_at:
        raise Http404
    retry_available = settings.DEV_BYPASS_PAYMENT and not job.audio_deleted_at and job.status in {AudioJob.Status.FAILED, AudioJob.Status.REFUND_PENDING}
    template = "jobs/_detail.html" if request.GET.get("partial") == "1" else "jobs/detail.html"
    return render(request, template, {"job": job, "dev_bypass_payment": settings.DEV_BYPASS_PAYMENT, "retry_available": retry_available, "job_state": status_payload(job)})

@login_required
@never_cache
def job_status(request, pk):
    job = owned_job(request, pk)
    if job.audio_deleted_at:
        raise Http404
    return JsonResponse(status_payload(job))

@login_required
@require_POST
def delete_audio(request, pk):
    with transaction.atomic():
        job = get_object_or_404(AudioJob.objects.select_for_update(), pk=pk, user=request.user)
        if job.audio_deleted_at is None:
            if default_storage.exists(job.original_key):
                default_storage.delete(job.original_key)
            if default_storage.exists(job.original_key):
                messages.error(request, "Não foi possível excluir o áudio agora. Tente novamente.")
                return redirect("job_detail", pk=pk)
            from django.utils import timezone
            job.audio_deleted_at = timezone.now()
            if job.can_transition(AudioJob.Status.CANCELLED):
                job.transition(AudioJob.Status.CANCELLED)
            else:
                job.save(update_fields=["audio_deleted_at"])
            messages.success(request, "Áudio excluído permanentemente.")
    return redirect("dashboard")

@login_required
@require_POST
def retry_transcription(request, pk):
    if not settings.DEV_BYPASS_PAYMENT:
        raise Http404
    try:
        with transaction.atomic():
            job = get_object_or_404(AudioJob.objects.select_for_update(), pk=pk, user=request.user)
            if job.status not in {AudioJob.Status.FAILED, AudioJob.Status.REFUND_PENDING}:
                raise RetryPreparationError("Este pedido não está disponível para nova tentativa.")
            if job.audio_deleted_at or not default_storage.exists(job.original_key):
                raise RetryPreparationError("O áudio original foi excluído e não pode ser reprocessado.")
            if not job.duration_seconds:
                raise RetryPreparationError("A duração do áudio não foi validada. Exclua o pedido e envie o arquivo novamente.")

            from payments.models import Payment
            from django.utils import timezone

            try:
                quote = job.quote
            except AudioJob.quote.RelatedObjectDoesNotExist:
                try:
                    quote = create_quote(job)
                except ValueError as exc:
                    raise RetryPreparationError(str(exc)) from exc

            payment = Payment.objects.filter(job=job).first()
            if payment is not None and (
                payment.status != Payment.Status.RECEIVED or payment.amount_brl != quote.total_brl
            ):
                raise RetryPreparationError("Já existe um pagamento incompatível com esta cotação.")
            if payment is None:
                payment = Payment.objects.create(
                    job=job,
                    amount_brl=quote.total_brl,
                    status=Payment.Status.RECEIVED,
                    billing_type="DEVELOPMENT",
                    remote_id=f"dev_{uuid.uuid4().hex}",
                )
            if quote.accepted_at is None:
                quote.accepted_at = timezone.now()
                quote.save(update_fields=["accepted_at"])

            job.segments.all().delete()
            job.speakers.all().delete()
            if hasattr(job, "usage"):
                job.usage.delete()
            job.needs_review, job.review_reasons, job.quality_metadata = False, [], {}
            job.status, job.failure_reason = AudioJob.Status.PAID_QUEUED, ""
            job.processing_started_at, job.processing_finished_at = None, None
            job.save(update_fields=["status", "failure_reason", "processing_started_at", "processing_finished_at", "updated_at", "needs_review", "review_reasons", "quality_metadata"])
            from .tasks import transcribe_job
            transaction.on_commit(lambda: transcribe_job.delay(str(job.id)))
    except RetryPreparationError as exc:
        logger.warning("Retry blocked for job %s: %s", pk, exc)
        messages.error(request, str(exc))
        return redirect("job_detail", pk=pk)
    logger.info("Retry queued for job %s", pk)
    messages.success(request, "Nova tentativa iniciada.")
    return redirect("job_detail", pk=pk)

def timestamp(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{milli:03}"

@login_required
def export(request, pk, fmt):
    job = owned_job(request, pk)
    if job.status != AudioJob.Status.READY or job.audio_deleted_at:
        raise Http404
    segs = job.segments.select_related("speaker")
    if fmt == "txt":
        body = "\n".join(f"[{timestamp(s.start_ms)} - {timestamp(s.end_ms)}] {s.speaker.display_name if s.speaker else 'Falante não identificado'}: {'[Revisão pendente] ' if s.needs_review else ''}{s.text}" for s in segs)
        if job.needs_review:
            body = "REVISÃO PENDENTE: confira os trechos sinalizados com o áudio original.\n" + "\n".join(f"[{n['start']} - {n['end']}] {n['label']}" for n in job.review_notices) + "\n\n" + body
        content_type = "text/plain"
    elif fmt == "srt":
        cues = []
        for i, segment in enumerate(segs, 1):
            warning = "[Revisão pendente] " if segment.needs_review or (i == 1 and job.needs_review) else ""
            speaker = segment.speaker.display_name if segment.speaker else "Falante não identificado"
            cues.append(f"{i}\n{timestamp(segment.start_ms)} --> {timestamp(segment.end_ms)}\n{speaker}: {warning}{segment.text}")
        if not cues and job.needs_review:
            cues.append("1\n00:00:00,000 --> 00:00:01,000\n[Revisão pendente: confira o áudio original; nenhum texto disponível.]")
        body = "\n\n".join(cues)
        content_type = "application/x-subrip"
    elif fmt == "pdf":
        from .pdf import build_transcript_pdf
        response = HttpResponse(build_transcript_pdf(job), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="transcricao-{job.id}.pdf"'
        return response
    else:
        raise Http404
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{job.id}.{fmt}"'
    return response

@login_required
@require_POST
def save_review(request, pk):
    job = owned_job(request, pk)
    for speaker in job.speakers.all():
        name = request.POST.get(f"speaker_{speaker.id}")
        if name:
            speaker.display_name = name[:100]
            speaker.save(update_fields=["display_name"])
    for segment in job.segments.all():
        text = request.POST.get(f"segment_{segment.id}")
        if text is not None:
            segment.revised_text = text
            segment.save(update_fields=["revised_text"])
    if request.POST.get("confirm_quality_review") == "1":
        job.segments.update(needs_review=False, review_reasons=[])
        job.needs_review, job.review_reasons = False, []
        job.save(update_fields=["needs_review", "review_reasons", "updated_at"])
    messages.success(request, "Revisão salva.")
    return redirect("job_detail", pk=pk)
