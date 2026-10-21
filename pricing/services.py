from decimal import Decimal, ROUND_CEILING
from .models import PricingConfig

def active_config(provider="openai"):
    config = PricingConfig.objects.filter(active=True, provider=provider).order_by("-created_at").first()
    if not config or not config.is_chargeable(): raise ValueError("A precificação ainda não foi validada por um administrador.")
    return config

def quote_for_seconds(seconds, config=None, provider="openai"):
    config = config or active_config(provider); minutes = Decimal(str(seconds)) / Decimal("60")
    value = lambda field: Decimal(str(getattr(config, field)))
    api_usd = Decimal("0") if config.provider == "deepgram" else ((minutes * value("audio_tokens_per_minute") * value("input_usd_per_million")) + (minutes * value("output_tokens_per_minute") * value("output_usd_per_million"))) / Decimal("1000000")
    if config.provider == "deepgram":
        api_usd = minutes * value("api_usd_per_minute")
    if config.verification_usd_per_minute is not None:
        api_usd += minutes * Decimal("0.20") * value("verification_usd_per_minute")
    api_brl = api_usd * value("usd_brl"); infra = minutes * value("infra_brl_per_minute"); base = api_brl + infra
    total = ((base * (Decimal("1") + value("margin")) + value("gateway_fixed_brl")) / (Decimal("1") - value("gateway_percent"))).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return {"config": config, "duration_seconds": seconds, "api_brl": api_brl.quantize(Decimal("0.0001")), "infra_brl": infra.quantize(Decimal("0.0001")), "total_brl": total}
