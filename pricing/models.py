import uuid
from django.conf import settings
from django.db import models

class PricingConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    active = models.BooleanField(default=False)
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
        fields = ("usd_brl", "audio_tokens_per_minute", "output_tokens_per_minute", "input_usd_per_million", "output_usd_per_million", "infra_brl_per_minute", "gateway_fixed_brl", "gateway_percent", "margin")
        return self.active and self.validated_at and all(getattr(self, f) is not None for f in fields) and self.gateway_percent < 1
