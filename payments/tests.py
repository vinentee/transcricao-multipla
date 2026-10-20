import json
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from jobs.models import AudioJob, Quote
from .models import Payment, WebhookEvent

@override_settings(ASAAS_WEBHOOK_TOKEN="webhook-test")
class WebhookTests(TestCase):
    def setUp(self):
        user=get_user_model().objects.create_user(email="pagador@example.com", password="senha")
        self.job=AudioJob.objects.create(user=user, status=AudioJob.Status.AWAITING_PAYMENT, original_key="a", original_filename="a.wav")
        Quote.objects.create(job=self.job, duration_seconds=60, total_brl="10.00", api_brl="1", infra_brl="1", pricing_snapshot={}, expires_at="2030-01-01T00:00:00Z")
        Payment.objects.create(job=self.job, remote_id="pay_1", amount_brl=Decimal("10.00"), status=Payment.Status.PENDING)
    @patch("payments.views.process_webhook_task.delay")
    def test_duplicate_webhook_is_persisted_once(self, delay):
        payload={"id":"evt_1", "event":"PAYMENT_RECEIVED", "payment":{"id":"pay_1", "value":10, "billingType":"PIX", "status":"RECEIVED"}}
        headers={"HTTP_ASAAS_ACCESS_TOKEN":"webhook-test", "content_type":"application/json"}
        self.assertEqual(self.client.post("/pagamentos/webhook/asaas/", data=json.dumps(payload), **headers).status_code, 200)
        self.assertEqual(self.client.post("/pagamentos/webhook/asaas/", data=json.dumps(payload), **headers).status_code, 200)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        delay.assert_called_once_with("evt_1")

@override_settings(DEBUG=True, DEV_BYPASS_PAYMENT=True)
class DevelopmentPaymentTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user(email="dev@example.com", password="senha")
        self.job=AudioJob.objects.create(user=self.user, status=AudioJob.Status.QUOTED, original_key="a", original_filename="a.wav")
        Quote.objects.create(job=self.job, duration_seconds=60, total_brl="10.00", api_brl="1", infra_brl="1", pricing_snapshot={}, expires_at="2030-01-01T00:00:00Z")
        self.client.force_login(self.user)

    @patch("payments.views.transcribe_job", create=True)
    def test_development_payment_queues_once(self, task):
        task.delay = lambda *args: None
        url=f"/pagamentos/pedidos/{self.job.id}/pagamento-teste/"
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, AudioJob.Status.PAID_QUEUED)
        self.assertEqual(Payment.objects.filter(job=self.job, status=Payment.Status.RECEIVED).count(), 1)

    @override_settings(DEV_BYPASS_PAYMENT=False)
    def test_bypass_is_hidden_when_disabled(self):
        response=self.client.post(f"/pagamentos/pedidos/{self.job.id}/pagamento-teste/")
        self.assertEqual(response.status_code, 404)
