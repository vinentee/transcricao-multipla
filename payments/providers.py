from abc import ABC, abstractmethod
from datetime import date
import requests
from django.conf import settings

class PaymentGateway(ABC):
    @abstractmethod
    def create_charge(self, payment): ...
    @abstractmethod
    def refund(self, payment): ...
    @abstractmethod
    def get_charge(self, remote_id): ...

class AsaasPaymentGateway(PaymentGateway):
    def _request(self, method, path, **kwargs):
        response = requests.request(method, settings.ASAAS_API_URL + path, headers={"access_token": settings.ASAAS_API_KEY, "Content-Type": "application/json"}, timeout=20, **kwargs)
        response.raise_for_status(); return response.json()
    def create_charge(self, payment):
        data = self._request("POST", "/payments", json={"customer": payment.remote_customer_id, "billingType": "UNDEFINED", "value": float(payment.amount_brl), "dueDate": date.today().isoformat(), "description": f"Transcrição {payment.job_id}", "externalReference": str(payment.job_id)})
        return {"id": data["id"], "url": data.get("invoiceUrl", "")}
    def ensure_customer(self, user):
        if user.asaas_customer_id:
            return user.asaas_customer_id
        data = self._request("POST", "/customers", json={"name": user.email, "email": user.email, "externalReference": str(user.id)})
        user.asaas_customer_id = data["id"]
        user.save(update_fields=["asaas_customer_id"])
        return user.asaas_customer_id
    def refund(self, payment): return self._request("POST", f"/payments/{payment.remote_id}/refund", json={})
    def get_charge(self, remote_id): return self._request("GET", f"/payments/{remote_id}")
