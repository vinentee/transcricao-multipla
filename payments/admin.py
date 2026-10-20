from django.contrib import admin
from .models import Payment, WebhookEvent
admin.site.register([Payment, WebhookEvent])
