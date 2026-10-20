from django.contrib.auth import views as auth_views
from django.urls import path
from .views import signup
urlpatterns = [path("cadastro/", signup, name="signup"), path("entrar/", auth_views.LoginView.as_view(template_name="accounts/login.html"), name="login"), path("sair/", auth_views.LogoutView.as_view(), name="logout"), path("senha/", auth_views.PasswordResetView.as_view(template_name="accounts/password_reset.html"), name="password_reset")]
