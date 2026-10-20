import uuid
from django.db import models
from jobs.models import AudioJob

class Payment(models.Model):
    class Status(models.TextChoices):
        CREATED="created", "Criado"; PENDING="pending", "Pendente"; RECEIVED="received", "Recebido"; REFUND_PENDING="refund_pending", "Estorno pendente"; REFUNDED="refunded", "Estornado"; FAILED="failed", "Falhou"; EXPIRED="expired", "Expirado"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False); job = models.OneToOneField(AudioJob, on_delete=models.PROTECT, related_name="payment")
    created_at = models.DateTimeField(auto_now_add=True); updated_at = models.DateTimeField(auto_now=True); status = models.CharField(max_length=24, choices=Status.choices, default=Status.CREATED)
    remote_id = models.CharField(max_length=100, unique=True, null=True, blank=True); remote_customer_id = models.CharField(max_length=100, blank=True); billing_type = models.CharField(max_length=30, default="UNDEFINED")
    amount_brl = models.DecimalField(max_digits=12, decimal_places=2); checkout_url = models.URLField(blank=True); effective_fee_brl = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True); refund_remote_id = models.CharField(max_length=100, blank=True)

class WebhookEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False); remote_event_id = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True); processed_at = models.DateTimeField(null=True, blank=True); payload = models.JSONField(); processing_error = models.CharField(max_length=300, blank=True)
