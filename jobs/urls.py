from django.urls import path
from . import views

urlpatterns = [
    path("pedidos/status/", views.jobs_status, name="jobs_status"),
    path("", views.dashboard, name="dashboard"), path("upload/", views.upload, name="upload"),
    path("pedidos/<uuid:pk>/", views.job_detail, name="job_detail"), path("pedidos/<uuid:pk>/status/", views.job_status, name="job_status"),
    path("pedidos/<uuid:pk>/audio/excluir/", views.delete_audio, name="delete_audio"), path("pedidos/<uuid:pk>/tentar-novamente/", views.retry_transcription, name="retry_transcription"), path("pedidos/<uuid:pk>/exportar/<str:fmt>/", views.export, name="export"),
    path("pedidos/<uuid:pk>/audio/", views.playback_audio, name="playback_audio"),
    path("pedidos/<uuid:pk>/audio/status/", views.playback_status, name="playback_status"),
    path("pedidos/<uuid:pk>/audio/preparar/", views.request_playback, name="request_playback"),
    path("pedidos/<uuid:pk>/renomear/", views.rename_job, name="rename_job"),
    path("pedidos/<uuid:pk>/revisao/", views.save_review, name="save_review"),
]
