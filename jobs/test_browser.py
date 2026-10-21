"""Optional browser regression: set PLAYWRIGHT_MODULE to a Playwright installation."""
import json
import os
from pathlib import Path
import subprocess
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AudioJob, Quote, Speaker, TranscriptSegment


@skipUnless(os.getenv("PLAYWRIGHT_MODULE"), "PLAYWRIGHT_MODULE is required for browser verification")
@override_settings(DEV_BYPASS_PAYMENT=True)
class ProgressBrowserTests(TestCase):
    def test_live_progress_and_reconnection(self):
        user = get_user_model().objects.create_user(email="browser@example.com", password="test-password")
        self.client.force_login(user)
        job = AudioJob.objects.create(user=user, status=AudioJob.Status.VALIDATING, original_key="browser.wav", original_filename="browser.wav")
        detail_url = reverse("job_detail", args=[job.id])
        status_url = reverse("job_status", args=[job.id])
        initial = self.client.get(detail_url).content.decode()
        dashboard = self.client.get(reverse("dashboard")).content.decode()
        fixtures = {}
        for status in [AudioJob.Status.VALIDATING, AudioJob.Status.QUOTED, AudioJob.Status.PAID_QUEUED, AudioJob.Status.PROCESSING, AudioJob.Status.READY, AudioJob.Status.FAILED]:
            job.status = status
            job.save()
            if status == AudioJob.Status.QUOTED:
                Quote.objects.create(job=job, duration_seconds=60, total_brl="10", api_brl="1", infra_brl="1", pricing_snapshot={}, expires_at=timezone.now() + timezone.timedelta(minutes=30))
            if status == AudioJob.Status.READY:
                speaker = Speaker.objects.create(job=job, number=1, display_name="Falante 1")
                TranscriptSegment.objects.create(job=job, speaker=speaker, ordinal=1, start_ms=0, end_ms=1000, raw_text="Texto de teste")
            if status == AudioJob.Status.FAILED:
                job.failure_reason = "Falha simulada"
                job.save()
            fixtures[status] = {
                "status": self.client.get(status_url).json(),
                "fragment": self.client.get(detail_url + "?partial=1").content.decode(),
                "page": self.client.get(detail_url).content.decode(),
            }
        script = Path(__file__).with_name("browser_progress.cjs")
        result = subprocess.run(
            ["node", str(script), os.environ["PLAYWRIGHT_MODULE"]],
            input=json.dumps({"initial": initial, "dashboard": dashboard, "fixtures": fixtures, "detailUrl": detail_url, "statusUrl": status_url, "root": str(settings.BASE_DIR)}),
            text=True, capture_output=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@skipUnless(os.getenv("PLAYWRIGHT_MODULE"), "PLAYWRIGHT_MODULE is required for browser verification")
class ReviewBrowserTests(TestCase):
    def test_review_layout_playback_and_edit_preservation(self):
        import base64
        import tempfile
        from .test_quality import write_wave
        user = get_user_model().objects.create_user(email="review-browser@example.com", password="test-password")
        self.client.force_login(user)
        job = AudioJob.objects.create(user=user, status=AudioJob.Status.READY, original_key="review.wav", original_filename="Conversa de teste.wav", duration_seconds=120, playback_key="review.mp3", playback_status="ready", needs_review=True)
        Quote.objects.create(job=job, duration_seconds=120, total_brl="10", api_brl="1", infra_brl="1", pricing_snapshot={}, expires_at=timezone.now() + timezone.timedelta(minutes=30))
        first = Speaker.objects.create(job=job, number=1, display_name="Falante 1")
        second = Speaker.objects.create(job=job, number=2, display_name="Falante 2")
        segments = []
        for index in range(30):
            start = 2000 + index * 4000
            segments.append(TranscriptSegment.objects.create(job=job, speaker=first if index % 2 == 0 else second, ordinal=index + 1, start_ms=start, end_ms=start + 2000,
                raw_text="Vamos conferir como o texto e os falantes aparecem nesta conversa.", needs_review=index % 2 == 0, review_reasons=["english", "verification_unresolved"] if index % 2 == 0 else []))
        job.review_reasons = [{"code": code, "start": s.start_ms / 1000, "end": s.end_ms / 1000} for s in segments for code in s.review_reasons]
        job.save()
        url = reverse("job_detail", args=[job.pk])
        html = self.client.get(url).content.decode()
        with tempfile.TemporaryDirectory() as directory:
            wav, mp3 = str(Path(directory) / "source.wav"), str(Path(directory) / "source.mp3")
            write_wave(wav, 12)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", wav, "-c:a", "libmp3lame", "-b:a", "64k", mp3], check=True, capture_output=True)
            audio = base64.b64encode(Path(mp3).read_bytes()).decode()
        result = subprocess.run(["node", str(Path(__file__).with_name("browser_review.cjs")), os.environ["PLAYWRIGHT_MODULE"]], input=json.dumps({
            "root": str(settings.BASE_DIR), "html": html, "detailUrl": url,
            "mediaStatusUrl": reverse("playback_status", args=[job.pk]), "mediaUrl": reverse("playback_audio", args=[job.pk]),
            "prepareUrl": reverse("request_playback", args=[job.pk]), "audio": audio,
            "secondSpeaker": str(second.pk), "firstTextName": f"segment_{segments[0].pk}",
        }), text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
