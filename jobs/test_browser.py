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
