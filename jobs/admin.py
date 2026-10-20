from django.contrib import admin
from .models import AudioJob, Quote, Speaker, TranscriptSegment, UsageRecord
admin.site.register([AudioJob, Quote, Speaker, TranscriptSegment, UsageRecord])
