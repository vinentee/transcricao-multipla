from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AudioJob, Quote, TranscriptSegment


class RenameJobTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="rename@example.com", password="password")
        self.client.force_login(self.user)
        self.job = AudioJob.objects.create(user=self.user, status=AudioJob.Status.READY,
            original_filename="gravação.wav", original_key="private/original.wav")
        self.url = reverse("rename_job", args=[self.job.pk])

    def test_rename_preserves_original_review_and_accepted_price(self):
        quote = Quote.objects.create(job=self.job, duration_seconds=60, total_brl="12.00",
            api_brl="1", infra_brl="1", pricing_snapshot={}, accepted_at=timezone.now(),
            expires_at=timezone.now() + timezone.timedelta(days=1))
        segment = TranscriptSegment.objects.create(job=self.job, ordinal=1, start_ms=0,
            end_ms=1000, raw_text="Original", corrected_text="Revisado")
        response = self.client.post(self.url, {"title": "  Reunião de outubro  "}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Reunião de outubro")
        self.job.refresh_from_db()
        self.assertEqual(self.job.title, "Reunião de outubro")
        self.assertEqual(self.job.original_filename, "gravação.wav")
        self.assertEqual(self.job.original_key, "private/original.wav")
        self.assertEqual(self.job.status, AudioJob.Status.READY)
        segment.refresh_from_db()
        self.assertEqual(segment.text, "Revisado")
        quote.refresh_from_db()
        self.assertEqual(str(quote.total_brl), "12.00")
        self.assertIsNotNone(quote.accepted_at)
        for url in (reverse("dashboard"), reverse("job_detail", args=[self.job.pk])):
            self.assertContains(self.client.get(url), "Reunião de outubro")
        self.assertEqual(self.client.get(reverse("job_status", args=[self.job.pk])).json()["title"], "Reunião de outubro")

    def test_validation_and_escaping(self):
        for title in ("", "   ", "x" * 256):
            response = self.client.post(self.url, {"title": title}, HTTP_ACCEPT="application/json")
            self.assertEqual(response.status_code, 400)
            self.assertIn("title", response.json()["errors"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.title, "")
        self.client.post(self.url, {"title": '<script>alert("x")</script>'})
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>alert("x")</script>')

    def test_access_control_and_csrf(self):
        other = get_user_model().objects.create_user(email="other-rename@example.com", password="password")
        self.client.force_login(other)
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(self.url, {"title": "Outro"}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"title": "Outro"}).status_code, 302)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(protected.post(self.url, {"title": "Outro"}).status_code, 403)
        self.client.force_login(self.user)
        self.job.audio_deleted_at = timezone.now()
        self.job.save()
        self.assertEqual(self.client.post(self.url, {"title": "Outro"}).status_code, 404)

    def test_fallback_and_existing_jobs(self):
        self.assertEqual(self.job.display_title, self.job.original_filename)
        self.assertContains(self.client.get(self.url), self.job.original_filename)
        self.job.refresh_from_db()
        self.assertEqual(self.job.title, "")
        self.assertEqual(self.client.post(self.url, {"title": " "}).status_code, 400)
        self.assertRedirects(self.client.post(self.url, {"title": "Novo", "return_to": "dashboard"}), reverse("dashboard"))
        self.assertRedirects(self.client.post(self.url, {"title": "Novo 2", "return_to": "https://example.com"}), reverse("job_detail", args=[self.job.pk]))
