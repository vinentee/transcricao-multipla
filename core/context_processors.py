from django.conf import settings


def feature_flags(request):
    return {"allow_signups": settings.ALLOW_SIGNUPS}
