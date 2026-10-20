from django import forms

class UploadForm(forms.Form):
    file = forms.FileField()
    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > 500 * 1024 * 1024:
            raise forms.ValidationError("O limite é 500 MB.")
        return f
