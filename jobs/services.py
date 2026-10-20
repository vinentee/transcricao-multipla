import json, os, subprocess, tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from pricing.services import quote_for_seconds
from .models import AudioJob, Quote, Speaker, TranscriptSegment

ALLOWED_EXTENSIONS = {"mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "opus"}

def run_ffmpeg(args):
    return subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True, timeout=180, check=True)

@contextmanager
def prepared_transcription_chunks(job):
    """Yields temporary, OpenAI-compatible M4A chunks and removes them on exit."""
    with tempfile.TemporaryDirectory(prefix="transcription-") as workdir:
        source = os.path.join(workdir, "original" + Path(job.original_filename).suffix.lower())
        with default_storage.open(job.original_key, "rb") as remote, open(source, "wb") as local:
            for part in iter(lambda: remote.read(1024 * 1024), b""):
                local.write(part)
        normalized = os.path.join(workdir, "normalized.m4a")
        run_ffmpeg(["-i", source, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", normalized])
        duration = float(json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", normalized], capture_output=True, text=True, timeout=30, check=True).stdout)["format"]["duration"])
        chunk_seconds, overlap = settings.TRANSCRIPTION_CHUNK_SECONDS, settings.TRANSCRIPTION_OVERLAP_SECONDS
        chunks, start, index = [], 0.0, 0
        while start < duration:
            end = min(duration, start + chunk_seconds)
            output = os.path.join(workdir, f"chunk-{index:04}.m4a")
            run_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", normalized, "-c", "copy", output])
            chunks.append((output, start))
            if end >= duration:
                break
            start, index = end - overlap, index + 1
        yield chunks
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
    mapping = {}; ordinal = 0
    for item in segments:
        label = str(item.get("speaker", "unknown"))
        if label not in mapping:
            if len(mapping) >= 4: job.needs_review = True; continue
            number = len(mapping)+1; mapping[label] = Speaker.objects.create(job=job, number=number, display_name=f"Falante {number}", provider_label=label)
        start, end = max(0, int(item["start"]*1000)), max(0, int(item["end"]*1000))
        if end <= start: continue
        ordinal += 1; TranscriptSegment.objects.create(job=job, speaker=mapping[label], ordinal=ordinal, start_ms=start, end_ms=end, raw_text=item.get("text", ""), needs_review=item.get("needs_review", False))
    job.save(update_fields=["needs_review"])
