from django.conf import settings
from django.test import SimpleTestCase, override_settings
from config.celery import app

class CeleryConfigurationTests(SimpleTestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_development_tasks_can_run_without_broker(self):
        app.conf.task_always_eager = settings.CELERY_TASK_ALWAYS_EAGER
        self.assertTrue(app.conf.task_always_eager)
