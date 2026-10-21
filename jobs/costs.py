"""Actual API estimates use the accepted quote's immutable tariff snapshot."""
from decimal import Decimal, InvalidOperation


def estimate_cost(snapshot, usages):
    try:
        total = Decimal("0")
        for usage in usages:
            minutes = Decimal(str(usage["audio_seconds"])) / 60
            if usage["role"] == "verification":
                total += minutes * Decimal(snapshot["verification_usd_per_minute"])
            elif usage["provider"] == "deepgram":
                total += minutes * Decimal(snapshot["api_usd_per_minute"])
            else:
                if usage.get("reported") is False:
                    return None
                total += (Decimal(str(usage["input_tokens"])) * Decimal(snapshot["input_usd_per_million"]) +
                          Decimal(str(usage["output_tokens"])) * Decimal(snapshot["output_usd_per_million"])) / 1000000
        return (total * Decimal(snapshot["usd_brl"])).quantize(Decimal("0.0001"))
    except (KeyError, InvalidOperation, TypeError):
        # Old quotes or incomplete tariffs cannot produce an honest estimate.
        return None
