import logging
import uuid
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_http_methods
from .forms import UploadForm
from .models import AudioJob, Speaker
from .services import create_quote
from .media import delete_media, playback_response
from .quality import collect_issues
from .providers import require_provider
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
        "playback_status": job.playback_status,
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
    job = get_object_or_404(AudioJob.objects.select_related("quote").prefetch_related(
        Prefetch("speakers", queryset=Speaker.objects.order_by("number"))
    ), pk=pk, user=request.user)
    if job.audio_deleted_at:
        raise Http404
    retry_available = settings.DEV_BYPASS_PAYMENT and not job.audio_deleted_at and job.status in {AudioJob.Status.FAILED, AudioJob.Status.REFUND_PENDING}
    template = "jobs/_detail.html" if request.GET.get("partial") == "1" else "jobs/detail.html"
    all_segments = list(job.segments.select_related("speaker"))
    segments = [segment for segment in all_segments if segment.excluded_at is None]
    excluded_segments = [segment for segment in all_segments if segment.excluded_at is not None]
    intervals = {(segment.start_ms // 1000, segment.end_ms // 1000) for segment in all_segments}
    legacy_notices = [notice for issue, notice in zip(job.review_reasons, job.review_notices) if (int(issue["start"]), int(issue["end"])) not in intervals]
    pending_count = sum(segment.needs_review for segment in segments) + len({(notice["start"], notice["end"]) for notice in legacy_notices})
    return render(request, template, {"job": job, "segments": segments, "excluded_segments": excluded_segments, "pending_count": pending_count,
                                    "legacy_notices": legacy_notices, "dev_bypass_payment": settings.DEV_BYPASS_PAYMENT,
                                    "retry_available": retry_available, "job_state": status_payload(job)})

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
            if not delete_media(job):
                messages.error(request, "Não foi possível excluir os arquivos de áudio agora. Tente novamente.")
                return redirect("job_detail", pk=pk)
            job.playback_key, job.playback_status = "", ""
            from django.utils import timezone
            job.audio_deleted_at = timezone.now()
            if job.can_transition(AudioJob.Status.CANCELLED):
                job.transition(AudioJob.Status.CANCELLED)
            else:
                job.save(update_fields=["audio_deleted_at", "playback_key", "playback_status", "updated_at"])
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

            try:
                require_provider(job.transcription_provider or "openai")
            except ValueError as exc:
                raise RetryPreparationError(str(exc)) from exc
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
    segs = job.segments.filter(excluded_at__isnull=True).select_related("speaker")
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
    with transaction.atomic():
        job = get_object_or_404(AudioJob.objects.select_for_update(), pk=pk, user=request.user)
        if job.audio_deleted_at or job.status != AudioJob.Status.READY:
            raise Http404
        speakers = {str(speaker.pk): speaker for speaker in job.speakers.all()}
        new_names = {key.removeprefix("new_speaker_"): value.strip() for key, value in request.POST.items() if key.startswith("new_speaker_")}
        import re
        if len(new_names) > 20 or any(not re.fullmatch(r"new_\d+", token) or not name or len(name) > 100 for token, name in new_names.items()):
            return JsonResponse({"error": "Novos falantes inválidos."}, status=400)
        # Validate all assignments before saving any edits.
        for segment in job.segments.all():
            if request.POST.get(f"delete_segment_{segment.pk}") == "1" and request.POST.get(f"restore_segment_{segment.pk}") == "1":
                return JsonResponse({"error": "Não é possível excluir e restaurar o mesmo trecho ao mesmo tempo."}, status=400)
            key = f"assign_{segment.pk}"
            if key in request.POST and request.POST[key] and request.POST[key] not in speakers and request.POST[key] not in new_names:
                return JsonResponse({"error": "Falante inválido para este pedido."}, status=400)
        next_number = max((speaker.number for speaker in speakers.values()), default=0) + 1
        from .models import Speaker
        for token, name in new_names.items():
            speakers[token] = Speaker.objects.create(job=job, number=next_number, display_name=name)
            next_number += 1
        for speaker in speakers.values():
            name = request.POST.get(f"speaker_{speaker.pk}")
            if name and name.strip():
                speaker.display_name = name.strip()[:100]
                speaker.save(update_fields=["display_name"])
        confirm_all = request.POST.get("confirm_quality_review") == "1"
        segments = list(job.segments.all())
        for segment in segments:
            changed = False
            deleting = request.POST.get(f"delete_segment_{segment.pk}") == "1"
            restoring = request.POST.get(f"restore_segment_{segment.pk}") == "1"
            # An archived segment is read-only until explicitly restored.
            if segment.excluded_at is not None and not restoring:
                continue
            if restoring and segment.excluded_at is not None:
                segment.excluded_at = None
                changed = True
            if deleting and segment.excluded_at is None:
                segment.excluded_at = timezone.now()
                changed = True
            text = request.POST.get(f"segment_{segment.pk}")
            if text is not None:
                segment.revised_text = text
                segment.has_manual_revision = True
                changed = True
            assignment = f"assign_{segment.pk}"
            if assignment in request.POST:
                changed = True
                segment.speaker = speakers.get(request.POST[assignment])
                # A manual assignment resolves identity uncertainty only.
                if segment.speaker:
                    segment.speaker_scope = "global"
                    segment.review_reasons = [code for code in segment.review_reasons if code not in {"unknown_speaker", "speaker_local"}]
                    segment.needs_review = bool(segment.review_reasons)
                else:
                    segment.speaker_scope = "unknown"
                    segment.review_reasons = sorted(set(segment.review_reasons) | {"unknown_speaker"})
                    segment.needs_review = True
            if segment.excluded_at is None and (confirm_all or request.POST.get(f"reviewed_{segment.pk}") == "1"):
                segment.needs_review, segment.review_reasons = False, []
                segment.reviewed_at = timezone.now()
                changed = True
            if changed:
                segment.save(update_fields=["excluded_at", "revised_text", "has_manual_revision", "speaker", "speaker_scope", "needs_review", "review_reasons", "reviewed_at"])
        if confirm_all:
            job.review_reasons = []
        else:
            # Legacy gap notices without a corresponding segment remain visible.
            represented = {(s.start_ms, s.end_ms) for s in segments}
            gaps = [issue for issue in job.review_reasons if issue["code"] == "missing_speech" and (round(issue["start"] * 1000), round(issue["end"] * 1000)) not in represented]
            job.review_reasons = collect_issues([
                {"start": s.start_ms / 1000, "end": s.end_ms / 1000, "review_reasons": s.review_reasons}
                for s in segments if s.needs_review and s.excluded_at is None
            ], gaps)
        job.needs_review = bool(job.review_reasons) or any(s.needs_review for s in segments if s.excluded_at is None)
        job.save(update_fields=["needs_review", "review_reasons", "updated_at"])
    messages.success(request, "Revisão salva.")
    return redirect("job_detail", pk=pk)


@login_required
@require_POST
def request_playback(request, pk):
    with transaction.atomic():
        job = get_object_or_404(AudioJob.objects.select_for_update(), pk=pk, user=request.user)
        if job.audio_deleted_at or job.status != AudioJob.Status.READY:
            raise Http404
        stale = job.playback_requested_at and job.playback_requested_at < timezone.now() - timezone.timedelta(minutes=15)
        if job.playback_status not in {"preparing", "ready"} or (job.playback_status == "preparing" and stale):
            job.playback_status, job.playback_requested_at = "preparing", timezone.now()
            job.save(update_fields=["playback_status", "playback_requested_at", "updated_at"])
            from .tasks import prepare_playback_job
            transaction.on_commit(lambda: prepare_playback_job.delay(str(job.pk)))
    return JsonResponse({"status": job.playback_status}, status=202)


@login_required
@never_cache
def playback_status(request, pk):
    job = owned_job(request, pk)
    if job.audio_deleted_at or job.status != AudioJob.Status.READY:
        raise Http404
    return JsonResponse({"status": job.playback_status, "url": reverse("playback_audio", args=[job.pk]) if job.playback_status == "ready" else None})


@login_required
@never_cache
@require_http_methods(["GET", "HEAD"])
def playback_audio(request, pk):
    job = owned_job(request, pk)
    if job.audio_deleted_at or job.status != AudioJob.Status.READY or job.playback_status != "ready" or not job.playback_key:
        raise Http404
    if not default_storage.exists(job.playback_key):
        raise Http404
    try:
        return playback_response(request, job)
    except FileNotFoundError:
        raise Http404 from None
