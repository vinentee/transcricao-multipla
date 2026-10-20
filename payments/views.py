import json
import uuid
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from jobs.models import AudioJob, Quote
from .models import Payment, WebhookEvent
from .services import create_payment
from .tasks import process_webhook_task

@login_required
@require_POST
def create_payment_view(request, job_id):
    job=get_object_or_404(AudioJob, pk=job_id, user=request.user)
    try: payment=create_payment(job)
    except ValueError as exc: return JsonResponse({"error":str(exc)}, status=400)
    return redirect(payment.checkout_url)

@login_required
@require_POST
def development_payment(request, job_id):
    """Development-only stand-in for a validated payment webhook."""
    if not settings.DEV_BYPASS_PAYMENT:
        return JsonResponse({"error": "Pagamento de teste indisponível."}, status=404)
    with transaction.atomic():
        job = get_object_or_404(AudioJob.objects.select_for_update(), pk=job_id, user=request.user)
        if job.status == AudioJob.Status.PAID_QUEUED:
            return redirect("job_detail", pk=job.id)
        quote = Quote.objects.filter(job=job).first()
        if job.status != AudioJob.Status.QUOTED or quote is None or quote.expired:
            return JsonResponse({"error": "Cotação indisponível ou expirada."}, status=400)
        payment, created = Payment.objects.get_or_create(
            job=job,
            defaults={"amount_brl": quote.total_brl, "status": Payment.Status.RECEIVED,
                      "billing_type": "DEVELOPMENT", "remote_id": f"dev_{uuid.uuid4().hex}"},
        )
        if not created and (
            payment.status != Payment.Status.RECEIVED or payment.amount_brl != quote.total_brl
        ):
            return JsonResponse({"error": "Já existe uma cobrança para este pedido."}, status=409)
        quote.accepted_at = quote.accepted_at or timezone.now()
        quote.save(update_fields=["accepted_at"])
        job.transition(AudioJob.Status.AWAITING_PAYMENT)
        job.transition(AudioJob.Status.PAID_QUEUED)
        from jobs.tasks import transcribe_job
        transaction.on_commit(lambda: transcribe_job.delay(str(job.id)))
    return redirect("job_detail", pk=job.id)

@csrf_exempt
@require_POST
def webhook(request):
    if not settings.ASAAS_WEBHOOK_TOKEN or request.headers.get("asaas-access-token") != settings.ASAAS_WEBHOOK_TOKEN: return HttpResponseForbidden()
    try: payload=json.loads(request.body); event_id=payload["id"]
    except (ValueError, KeyError): return JsonResponse({"error":"payload inválido"}, status=400)
    event, created=WebhookEvent.objects.get_or_create(remote_event_id=event_id, defaults={"payload":payload})
    if created: process_webhook_task.delay(event_id)
    return JsonResponse({"ok":True})
