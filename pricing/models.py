import uuid
from django.conf import settings
from django.db import models

class PricingConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    active = models.BooleanField(default=False)
    provider = models.CharField(max_length=16, default="openai", choices=[("openai", "OpenAI"), ("deepgram", "Deepgram")])
    api_usd_per_minute = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    verification_usd_per_minute = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    usd_brl = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    audio_tokens_per_minute = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    output_tokens_per_minute = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    input_usd_per_million = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    output_usd_per_million = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    infra_brl_per_minute = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    gateway_fixed_brl = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    gateway_percent = models.DecimalField(max_digits=7, decimal_places=6, null=True, blank=True)
    margin = models.DecimalField(max_digits=7, decimal_places=6, default="0.10")
    notes = models.TextField(blank=True)
    def is_chargeable(self):
        common = ("usd_brl", "infra_brl_per_minute", "gateway_fixed_brl", "gateway_percent", "margin")
        primary = ("api_usd_per_minute", "verification_usd_per_minute") if self.provider == "deepgram" else ("audio_tokens_per_minute", "output_tokens_per_minute", "input_usd_per_million", "output_usd_per_million")
        values = [getattr(self, field) for field in common + primary]
        return bool(self.active and self.validated_at and all(v is not None and v >= 0 for v in values) and self.gateway_percent < 1 and self.usd_brl > 0)
