from django.http import JsonResponse
from django.shortcuts import render
def health(request): return JsonResponse({"status":"ok"})
def legal(request, page):
    if page not in {"privacidade", "termos", "contato"}: page="contato"
    return render(request, f"legal/{page}.html")
