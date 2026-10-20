from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from pricing.models import PricingConfig

class Command(BaseCommand):
    help = "Cria uma tabela de preços exclusivamente para desenvolvimento local."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("A configuração de preços de teste só pode ser criada com DEBUG=true.")
        existing = PricingConfig.objects.filter(notes__startswith="DESENVOLVIMENTO:", active=True).first()
        if existing:
            self.stdout.write(self.style.WARNING(f"Tabela de teste já existe: {existing.id}"))
            return
        config = PricingConfig.objects.create(
            active=True, validated_at=timezone.now(), usd_brl="5.50",
            audio_tokens_per_minute="100", output_tokens_per_minute="10",
            input_usd_per_million="2.50", output_usd_per_million="10.00",
            infra_brl_per_minute="0.01", gateway_fixed_brl="0.00",
            gateway_percent="0", margin="0.10",
            notes="DESENVOLVIMENTO: valores ilustrativos; não usar para cobrança comercial.",
        )
        self.stdout.write(self.style.SUCCESS(f"Tabela de preços de desenvolvimento criada: {config.id}"))
