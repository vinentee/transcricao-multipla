import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Local development reads its configuration from this untracked file. In
# Railway/production, environment variables supplied by the platform prevail.
load_dotenv(BASE_DIR / ".env")
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key")
DEBUG = os.getenv("DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [h for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [origin for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if origin]

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "storages",
    "accounts", "jobs", "pricing", "payments", "core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
WSGI_APPLICATION = "config.wsgi.application"

if os.getenv("DATABASE_URL"):
    import urllib.parse
    u = urllib.parse.urlparse(os.environ["DATABASE_URL"])
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": u.path.lstrip("/"), "USER": u.username, "PASSWORD": u.password, "HOST": u.hostname, "PORT": u.port or 5432}}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [{"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"}, {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}]
LANGUAGE_CODE, TIME_ZONE, USE_I18N, USE_TZ = "pt-br", "America/Sao_Paulo", True, True
STATIC_URL, STATIC_ROOT = "/static/", BASE_DIR / "staticfiles"
MEDIA_URL, MEDIA_ROOT = "/media/", BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL, LOGIN_REDIRECT_URL, LOGOUT_REDIRECT_URL = "login", "dashboard", "login"
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST, EMAIL_PORT = os.getenv("EMAIL_HOST", ""), int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER, EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_USER", ""), os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@example.test")

CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() == "true"
CELERY_BEAT_SCHEDULE = {"cleanup-audio": {"task": "jobs.tasks.cleanup_expired_audio", "schedule": 3600}, "reconcile-payments": {"task": "payments.tasks.reconcile_pending_payments", "schedule": 900}}

S3_ENABLED = os.getenv("S3_ENABLED", "false").lower() == "true"
if S3_ENABLED:
    AWS_ACCESS_KEY_ID = os.environ["S3_ACCESS_KEY_ID"]
    AWS_SECRET_ACCESS_KEY = os.environ["S3_SECRET_ACCESS_KEY"]
    AWS_STORAGE_BUCKET_NAME = os.environ["S3_BUCKET"]
    AWS_S3_ENDPOINT_URL = os.environ["S3_ENDPOINT_URL"]
    AWS_S3_REGION_NAME = os.getenv("S3_REGION", "auto")
    AWS_S3_SIGNATURE_VERSION = "s3v4"
    AWS_DEFAULT_ACL = None
    STORAGES = {"default": {"BACKEND": "storages.backends.s3.S3Storage"}, "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TRANSCRIPTION_MODEL = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-transcribe-diarize")
DEV_BYPASS_PAYMENT = DEBUG and os.getenv("DEV_BYPASS_PAYMENT", "false").lower() == "true"
ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "")
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "")
ASAAS_API_URL = os.getenv("ASAAS_API_URL", "https://api-sandbox.asaas.com/v3")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))
MAX_AUDIO_SECONDS = 2 * 60 * 60
TRANSCRIPTION_CHUNK_SECONDS = int(os.getenv("TRANSCRIPTION_CHUNK_SECONDS", "240"))
TRANSCRIPTION_OVERLAP_SECONDS = int(os.getenv("TRANSCRIPTION_OVERLAP_SECONDS", "3"))
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "false").lower() == "true"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE = not DEBUG
