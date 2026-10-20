from django.contrib import admin
from django.utils import timezone
from .models import PricingConfig
@admin.register(PricingConfig)
class PricingConfigAdmin(admin.ModelAdmin):
    list_display = ("created_at", "active", "validated_at", "is_chargeable")
    readonly_fields = ("created_at",)
    actions = ["validate_configs"]
    @admin.action(description="Validar configurações selecionadas")
    def validate_configs(self, request, queryset):
        queryset.update(validated_at=timezone.now(), validated_by=request.user)
