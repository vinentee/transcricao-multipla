from django.urls import path
from .views import create_payment_view, development_payment, webhook
urlpatterns=[path("pedidos/<uuid:job_id>/criar/", create_payment_view, name="create_payment"), path("pedidos/<uuid:job_id>/pagamento-teste/", development_payment, name="development_payment"), path("webhook/asaas/", webhook, name="asaas_webhook")]
