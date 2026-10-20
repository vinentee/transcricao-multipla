from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User
@admin.register(User)
class CustomUserAdmin(UserAdmin):
    model = User
    ordering = ("email",)
    list_display = ("email", "is_staff", "email_confirmed")
    fieldsets = ((None, {"fields": ("email", "password")}), ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}), ("Status", {"fields": ("email_confirmed", "last_login")}),)
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)
