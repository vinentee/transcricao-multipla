from django.contrib import admin
from django.urls import include, path
from core.views import health, legal

urlpatterns = [path("admin/", admin.site.urls), path("contas/", include("accounts.urls")), path("", include("jobs.urls")), path("pagamentos/", include("payments.urls")), path("health/", health, name="health"), path("legal/<str:page>/", legal, name="legal")]
