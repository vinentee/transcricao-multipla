import json, subprocess, tempfile
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from pricing.services import quote_for_seconds
from .models import AudioJob, Quote, Speaker, TranscriptSegment
from .audio import prepare_chunks as prepared_transcription_chunks

ALLOWED_EXTENSIONS = {"mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "opus"}

def inspect_audio(job):
    suffix = Path(job.original_filename).suffix.lower().lstrip(".")
    if suffix not in ALLOWED_EXTENSIONS or job.file_size > settings.MAX_UPLOAD_BYTES: raise ValueError("Arquivo não permitido ou acima do limite.")
    with tempfile.NamedTemporaryFile(suffix="."+suffix) as temp:
        with default_storage.open(job.original_key, "rb") as source:
            for part in iter(lambda: source.read(1024*1024), b""): temp.write(part)
        temp.flush()
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", temp.name], capture_output=True, text=True, timeout=30, check=True)
    seconds = int(float(json.loads(probe.stdout)["format"]["duration"]))
    if seconds <= 0 or seconds > settings.MAX_AUDIO_SECONDS: raise ValueError("Duração inválida ou superior a duas horas.")
    return seconds
def create_quote(job):
    from .providers import freeze_provider
    freeze_provider(job)
    calc = quote_for_seconds(job.duration_seconds, provider=job.transcription_provider); c = calc["config"]
    snapshot = {field: str(getattr(c, field)) for field in ("usd_brl","audio_tokens_per_minute","output_tokens_per_minute","input_usd_per_million","output_usd_per_million","infra_brl_per_minute","gateway_fixed_brl","gateway_percent","margin")}
    snapshot.update({"provider": job.transcription_provider, "model": job.transcription_model, "api_usd_per_minute": str(c.api_usd_per_minute), "verification_usd_per_minute": str(c.verification_usd_per_minute)})
    return Quote.objects.create(job=job, duration_seconds=job.duration_seconds, total_brl=calc["total_brl"], api_brl=calc["api_brl"], infra_brl=calc["infra_brl"], pricing_snapshot=snapshot, expires_at=timezone.now()+timedelta(minutes=30))
def save_provider_segments(job, segments, group_turns=False):
    if group_turns:
        from .paragraphs import group_speaker_turns
        segments = group_speaker_turns(segments)
    mapping = {}
    for ordinal, item in enumerate(segments, 1):
        label = item.get("speaker")
        label = str(label) if label is not None else None
        if label is not None and label not in mapping:
            number = len(mapping) + 1
            mapping[label] = Speaker.objects.create(job=job, number=number, display_name=f"Falante {number}", provider_label=label)
        reasons = sorted(set(item.get("review_reasons", [])))
        if label is None:
            reasons = sorted(set(reasons) | {"unknown_speaker"})
        start, end = max(0, round(item["start"] * 1000)), max(0, round(item["end"] * 1000))
        if end <= start:
            end = start + 1
            reasons = sorted(set(reasons) | {"invalid_segment"})
        TranscriptSegment.objects.create(
            job=job, speaker=mapping.get(label), ordinal=ordinal, start_ms=start, end_ms=end,
            raw_text=item.get("raw_text", item.get("text", "")), corrected_text=item.get("corrected_text", ""),
            provider_label=item.get("provider_label", label or ""), speaker_scope=item.get("speaker_scope", "global" if label is not None else "unknown"),
            source_segments=item.get("source_segments", []), origin="grouped" if len(item.get("source_segments", [])) > 1 else "automatic",
            provider_words=item.get("words", []), alternatives=item.get("alternatives", []), needs_review=bool(reasons) or item.get("needs_review", False), review_reasons=reasons,
        )
        job.needs_review = job.needs_review or bool(reasons) or item.get("needs_review", False)
    job.save(update_fields=["needs_review", "review_reasons", "quality_metadata"])
