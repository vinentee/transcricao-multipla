"""Private playback media, independent from the source used by ASR."""
import os
import re
import subprocess
import uuid

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse, HttpResponse, HttpResponseRedirect, StreamingHttpResponse
from django.utils import timezone

from .audio import prepare_audio
from .models import AudioJob


def build_playback(job, pcm_path=None):
    with transaction.atomic():
        current = AudioJob.objects.select_for_update().get(pk=job.pk)
        if current.audio_deleted_at:
            return
        if current.playback_status == "ready" and current.playback_key:
            return
        current.playback_status = "preparing"
        current.playback_requested_at = timezone.now()
        current.save(update_fields=["playback_status", "playback_requested_at", "updated_at"])
    try:
        if pcm_path is None:
            with prepare_audio(job) as prepared:
                return _encode_and_link(job, prepared.path)
        return _encode_and_link(job, pcm_path)
    except Exception:
        AudioJob.objects.filter(pk=job.pk, audio_deleted_at__isnull=True).update(playback_status="failed", updated_at=timezone.now())
        # Playback failure does not invalidate the transcript. Do not expose
        # subprocess/provider messages, which can contain private paths.
        return None


def _encode_and_link(job, pcm_path):
    output = os.path.join(os.path.dirname(pcm_path), "playback.mp3")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", pcm_path, "-vn", "-c:a", "libmp3lame", "-b:a", "64k", output], capture_output=True, timeout=600, check=True)
    with open(output, "rb") as source:
        key = default_storage.save(f"playback/{job.pk}/{uuid.uuid4().hex}.mp3", File(source))
    try:
        with transaction.atomic():
            current = AudioJob.objects.select_for_update().get(pk=job.pk)
            if current.audio_deleted_at:
                default_storage.delete(key)
                return
            previous = current.playback_key
            current.playback_key, current.playback_status = key, "ready"
            current.save(update_fields=["playback_key", "playback_status", "updated_at"])
        if previous and previous != key:
            default_storage.delete(previous)
        return key
    except Exception:
        default_storage.delete(key)
        raise


def delete_media(job):
    keys = [key for key in (job.original_key, job.playback_key) if key]
    for key in keys:
        default_storage.delete(key)
    return all(not default_storage.exists(key) for key in keys)


def playback_response(request, job):
    if settings.S3_ENABLED:
        return HttpResponseRedirect(default_storage.url(job.playback_key, parameters={"ResponseContentType": "audio/mpeg"}, expire=300))
    size = default_storage.size(job.playback_key)
    start, end, partial = 0, size - 1, False
    requested = request.headers.get("Range")
    if requested:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
        if not match or not any(match.groups()):
            return HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}"})
        left, right = match.groups()
        if left:
            start, end = int(left), min(int(right), size - 1) if right else size - 1
        else:
            start, end = max(0, size - int(right)), size - 1
        if start >= size or start > end:
            return HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}"})
        partial = True
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "private, no-store"}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return HttpResponse(status=206 if partial else 200, content_type="audio/mpeg", headers=headers)
    source = default_storage.open(job.playback_key, "rb")
    if not partial:
        response = FileResponse(source, content_type="audio/mpeg")
        for name, value in headers.items():
            response[name] = value
        return response
    source.seek(start)

    def stream():
        try:
            remaining = end - start + 1
            while remaining:
                data = source.read(min(64 * 1024, remaining))
                if not data:
                    break
                yield data
                remaining -= len(data)
        finally:
            source.close()
    response = StreamingHttpResponse(stream(), status=206, content_type="audio/mpeg", headers=headers)
    response._resource_closers.append(source.close)
    return response
