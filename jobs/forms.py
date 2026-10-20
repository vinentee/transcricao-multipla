from django import forms
from django.conf import settings

class UploadForm(forms.Form):
    file = forms.FileField()
    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > settings.MAX_UPLOAD_BYTES:
            limit_mb = settings.MAX_UPLOAD_BYTES / (1024 * 1024)
            raise forms.ValidationError(f"O limite é {limit_mb:g} MB.")
        return f
