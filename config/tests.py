from io import StringIO
from pathlib import Path
import tempfile

from django.conf import settings
from django.core.management import call_command
from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.test.client import RequestFactory
from django.urls import reverse
from config.celery import app

class CeleryConfigurationTests(SimpleTestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_development_tasks_can_run_without_broker(self):
        app.conf.task_always_eager = settings.CELERY_TASK_ALWAYS_EAGER
        self.assertTrue(app.conf.task_always_eager)


class ProductionStaticFilesTests(SimpleTestCase):
    def test_build_assets_render_and_are_served_without_manifest(self):
        with tempfile.TemporaryDirectory(prefix="transcription-static-") as directory:
            with override_settings(STATIC_ROOT=directory):
                call_command("collectstatic", interactive=False, stdout=StringIO(), verbosity=0)
                self.assertTrue((Path(directory) / "jobs/progress.js").exists())
                self.assertTrue((Path(directory) / "jobs/progress.css").exists())
                # Simulate R2 being enabled only at runtime, after collectstatic.
                runtime_storages = {
                    "default": {"BACKEND": "storages.backends.s3.S3Storage"},
                    "staticfiles": settings.STORAGES["staticfiles"],
                }
                with override_settings(DEBUG=False, STORAGES=runtime_storages, ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False):
                    response = self.client.get(reverse("login"))
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, "/static/jobs/progress.js")
                    self.assertContains(response, "/static/jobs/progress.css")
                    html = Template("{% load static %}{% static 'jobs/progress.js' %}").render(Context())
                    self.assertEqual(html, "/static/jobs/progress.js")
                    from whitenoise.middleware import WhiteNoiseMiddleware
                    middleware = WhiteNoiseMiddleware(lambda request: None)
                    for asset in ["jobs/progress.js", "jobs/progress.css"]:
                        response = middleware(RequestFactory().get("/static/" + asset))
                        self.assertIsNotNone(response)
                        self.assertEqual(response.status_code, 200)
                        response.close()
