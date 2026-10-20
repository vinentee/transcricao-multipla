from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from .forms import SignupForm

def signup(request):
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False); user.is_active = True; user.save()
            token = default_token_generator.make_token(user)
            send_mail("Confirme sua conta", f"Token de confirmação: {token}", None, [user.email])
            login(request, user); messages.success(request, "Conta criada. Confira o e-mail para o token de confirmação.")
            return redirect("dashboard")
    else: form = SignupForm()
    return render(request, "accounts/signup.html", {"form": form})
