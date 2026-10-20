from django.conf import settings
from django.db import transaction
from django.utils import timezone
from jobs.models import AudioJob
from .models import Payment, WebhookEvent
from .providers import AsaasPaymentGateway

def create_payment(job):
    if job.status != AudioJob.Status.QUOTED or job.quote.expired: raise ValueError("Cotação indisponível ou expirada.")
    if not settings.ASAAS_API_KEY: raise ValueError("Asaas não configurado.")
    gateway = AsaasPaymentGateway()
    customer_id = gateway.ensure_customer(job.user)
    payment, created = Payment.objects.get_or_create(job=job, defaults={"amount_brl": job.quote.total_brl, "remote_customer_id": customer_id})
    if created:
        data = AsaasPaymentGateway().create_charge(payment)
        payment.remote_id, payment.checkout_url, payment.status = data["id"], data["url"], Payment.Status.PENDING
        payment.save(update_fields=["remote_id","checkout_url","status","updated_at"])
        job.quote.accepted_at = timezone.now(); job.quote.save(update_fields=["accepted_at"]); job.transition(AudioJob.Status.AWAITING_PAYMENT)
    return payment

def process_webhook(event_id):
    with transaction.atomic():
        event = WebhookEvent.objects.select_for_update().get(remote_event_id=event_id)
        if event.processed_at: return
        resource = event.payload.get("payment", {}); remote_id = resource.get("id")
        payment = Payment.objects.select_for_update().filter(remote_id=remote_id).select_related("job", "job__quote").first()
        if not payment: event.processing_error="Cobrança desconhecida"; event.processed_at=timezone.now(); event.save(); return
        received = event.payload.get("event") in {"PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"} and resource.get("status") in {"RECEIVED", "CONFIRMED"}
        if received and str(resource.get("value")) == str(payment.amount_brl) and resource.get("billingType") in {"PIX", "CREDIT_CARD"} and payment.job.status == AudioJob.Status.AWAITING_PAYMENT and payment.job.audio_deleted_at is None:
            payment.status = Payment.Status.RECEIVED; payment.save(update_fields=["status","updated_at"]); payment.job.transition(AudioJob.Status.PAID_QUEUED)
            from jobs.tasks import transcribe_job
            transaction.on_commit(lambda: transcribe_job.delay(str(payment.job_id)))
        elif event.payload.get("event") in {"PAYMENT_OVERDUE", "PAYMENT_DELETED"}:
            payment.status=Payment.Status.EXPIRED; payment.save(update_fields=["status","updated_at"])
        event.processed_at=timezone.now(); event.save(update_fields=["processed_at","processing_error"])
