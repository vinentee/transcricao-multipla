from celery import shared_task
from .models import Payment
from .providers import AsaasPaymentGateway

@shared_task
def process_webhook_task(event_id):
    from .services import process_webhook
    return process_webhook(event_id)

@shared_task
def request_refund(job_id):
    payment = Payment.objects.filter(job_id=job_id, status=Payment.Status.RECEIVED).first()
    if not payment: return
    try:
        data = AsaasPaymentGateway().refund(payment); payment.status=Payment.Status.REFUND_PENDING; payment.refund_remote_id=data.get("id", ""); payment.save(update_fields=["status","refund_remote_id","updated_at"])
    except Exception: pass

@shared_task
def reconcile_pending_payments():
    gateway = AsaasPaymentGateway()
    for payment in Payment.objects.filter(status=Payment.Status.PENDING).exclude(remote_id__isnull=True):
        try:
            data=gateway.get_charge(payment.remote_id)
            if data.get("status") in {"RECEIVED", "CONFIRMED"}:
                from .services import process_webhook
                from .models import WebhookEvent
                event, _ = WebhookEvent.objects.get_or_create(remote_event_id=f"reconcile:{payment.remote_id}:{data['status']}", defaults={"payload":{"event":"PAYMENT_RECEIVED","payment":data}})
                process_webhook(event.remote_event_id)
        except Exception: continue
