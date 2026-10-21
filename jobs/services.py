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
    calc = quote_for_seconds(job.duration_seconds); c = calc["config"]
    snapshot = {field: str(getattr(c, field)) for field in ("usd_brl","audio_tokens_per_minute","output_tokens_per_minute","input_usd_per_million","output_usd_per_million","infra_brl_per_minute","gateway_fixed_brl","gateway_percent","margin")}
    return Quote.objects.create(job=job, duration_seconds=job.duration_seconds, total_brl=calc["total_brl"], api_brl=calc["api_brl"], infra_brl=calc["infra_brl"], pricing_snapshot=snapshot, expires_at=timezone.now()+timedelta(minutes=30))
def save_provider_segments(job, segments):
    mapping = {}
    for ordinal, item in enumerate(segments, 1):
        label = item.get("speaker")
        if label and label not in mapping:
            number = len(mapping) + 1
            mapping[label] = Speaker.objects.create(job=job, number=number, display_name=f"Falante {number}", provider_label=label)
        reasons = sorted(set(item.get("review_reasons", [])))
        if not label:
            reasons = sorted(set(reasons) | {"unknown_speaker"})
        start, end = max(0, round(item["start"] * 1000)), max(0, round(item["end"] * 1000))
        if end <= start:
            end = start + 1
            reasons = sorted(set(reasons) | {"invalid_segment"})
        TranscriptSegment.objects.create(
            job=job, speaker=mapping.get(label), ordinal=ordinal, start_ms=start, end_ms=end,
            raw_text=item.get("text", ""), needs_review=bool(reasons) or item.get("needs_review", False), review_reasons=reasons,
        )
        job.needs_review = job.needs_review or bool(reasons) or item.get("needs_review", False)
    job.save(update_fields=["needs_review", "review_reasons", "quality_metadata"])
