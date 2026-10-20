from abc import ABC, abstractmethod
from django.conf import settings
class TranscriptionProvider(ABC):
    @abstractmethod
    def transcribe(self, file_path, known_speakers=None): ...
class OpenAITranscriptionProvider(TranscriptionProvider):
    def transcribe(self, file_path, known_speakers=None):
        from openai import OpenAI
        with open(file_path, "rb") as audio:
            response = OpenAI(api_key=settings.OPENAI_API_KEY).audio.transcriptions.create(model=settings.OPENAI_TRANSCRIPTION_MODEL, file=audio, response_format="diarized_json", chunking_strategy="auto", extra_body=known_speakers or {})
        return {"segments": [dict(s) for s in response.segments], "usage": getattr(response, "usage", None)}
