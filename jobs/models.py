import uuid
from datetime import timedelta
from django.conf import settings
from django.db import models
from django.utils import timezone

class AudioJob(models.Model):
    class Status(models.TextChoices):
        UPLOADING="uploading", "Enviando"; VALIDATING="validating", "Validando"; QUOTED="quoted", "Cotado"; AWAITING_PAYMENT="awaiting_payment", "Aguardando pagamento"; PAID_QUEUED="paid_queued", "Pago / na fila"; PROCESSING="processing", "Processando"; READY="ready", "Pronto"; FAILED="failed", "Falhou"; REFUND_PENDING="refund_pending", "Estorno pendente"; REFUNDED="refunded", "Estornado"; CANCELLED="cancelled", "Cancelado"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="audio_jobs")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.UPLOADING)
    created_at = models.DateTimeField(auto_now_add=True); updated_at = models.DateTimeField(auto_now=True)
    original_key = models.CharField(max_length=512, unique=True); original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True); file_size = models.BigIntegerField(default=0); duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    audio_deleted_at = models.DateTimeField(null=True, blank=True); delete_audio_after = models.DateTimeField(null=True, blank=True)
    needs_review = models.BooleanField(default=False); failure_reason = models.CharField(max_length=300, blank=True)
    processing_started_at = models.DateTimeField(null=True, blank=True); processing_finished_at = models.DateTimeField(null=True, blank=True)
    class Meta: ordering = ["-created_at"]
    def can_transition(self, to):
        allowed = {self.Status.UPLOADING:{self.Status.VALIDATING,self.Status.CANCELLED}, self.Status.VALIDATING:{self.Status.QUOTED,self.Status.FAILED,self.Status.CANCELLED}, self.Status.QUOTED:{self.Status.AWAITING_PAYMENT,self.Status.CANCELLED}, self.Status.AWAITING_PAYMENT:{self.Status.PAID_QUEUED,self.Status.CANCELLED,self.Status.REFUND_PENDING}, self.Status.PAID_QUEUED:{self.Status.PROCESSING,self.Status.FAILED,self.Status.REFUND_PENDING,self.Status.CANCELLED}, self.Status.PROCESSING:{self.Status.READY,self.Status.FAILED,self.Status.REFUND_PENDING,self.Status.CANCELLED}, self.Status.REFUND_PENDING:{self.Status.REFUNDED,self.Status.FAILED,self.Status.CANCELLED}}
        return to in allowed.get(self.status, set())
    def transition(self, to, reason=""):
        if not self.can_transition(to): raise ValueError(f"Transição inválida: {self.status} → {to}")
        self.status, self.failure_reason = to, reason
        if to == self.Status.PROCESSING: self.processing_started_at = timezone.now()
        if to in (self.Status.READY, self.Status.CANCELLED): self.delete_audio_after = timezone.now() + timedelta(days=7)
        if to in (self.Status.READY, self.Status.FAILED, self.Status.REFUNDED): self.processing_finished_at = timezone.now()
        self.save()

class Quote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False); job = models.OneToOneField(AudioJob, on_delete=models.CASCADE, related_name="quote")
    created_at = models.DateTimeField(auto_now_add=True); expires_at = models.DateTimeField()
    duration_seconds = models.PositiveIntegerField(); total_brl = models.DecimalField(max_digits=12, decimal_places=2)
    api_brl = models.DecimalField(max_digits=12, decimal_places=4); infra_brl = models.DecimalField(max_digits=12, decimal_places=4)
    pricing_snapshot = models.JSONField(); accepted_at = models.DateTimeField(null=True, blank=True)
    @property
    def expired(self): return timezone.now() >= self.expires_at

class Speaker(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False); job = models.ForeignKey(AudioJob, on_delete=models.CASCADE, related_name="speakers")
    number = models.PositiveSmallIntegerField(); display_name = models.CharField(max_length=100); provider_label = models.CharField(max_length=100, blank=True); reference_key = models.CharField(max_length=512, blank=True)
    class Meta: unique_together = [("job", "number")]

class TranscriptSegment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False); job = models.ForeignKey(AudioJob, on_delete=models.CASCADE, related_name="segments")
    speaker = models.ForeignKey(Speaker, null=True, blank=True, on_delete=models.SET_NULL, related_name="segments")
    ordinal = models.PositiveIntegerField(); start_ms = models.PositiveIntegerField(); end_ms = models.PositiveIntegerField(); raw_text = models.TextField(); revised_text = models.TextField(blank=True); needs_review = models.BooleanField(default=False)
    class Meta: ordering = ["ordinal"]; unique_together = [("job", "ordinal")]
    @property
    def text(self): return self.revised_text or self.raw_text

class UsageRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False); job = models.OneToOneField(AudioJob, on_delete=models.CASCADE, related_name="usage")
    created_at = models.DateTimeField(auto_now_add=True); input_tokens = models.PositiveIntegerField(default=0); output_tokens = models.PositiveIntegerField(default=0)
    actual_cost_brl = models.DecimalField(max_digits=12, decimal_places=4, null=True); gateway_fee_brl = models.DecimalField(max_digits=12, decimal_places=2, null=True); charged_brl = models.DecimalField(max_digits=12, decimal_places=2); margin_brl = models.DecimalField(max_digits=12, decimal_places=4, null=True)
