"""Provider adapters. Remote error bodies are never exposed to logs or users."""
from abc import ABC, abstractmethod

import requests
from django.conf import settings


class ProviderError(RuntimeError):
    pass


def require_provider(provider):
    if provider not in {"openai", "deepgram"}:
        raise ValueError("Provedor de transcrição inválido.")
    key = settings.DEEPGRAM_API_KEY if provider == "deepgram" else settings.OPENAI_API_KEY
    if not key:
        raise ValueError(f"A chave do provedor {provider} não está configurada.")


def freeze_provider(job):
    if not job.transcription_provider:
        job.transcription_provider = settings.TRANSCRIPTION_PROVIDER
    require_provider(job.transcription_provider)
    if not job.transcription_model:
        job.transcription_model = settings.DEEPGRAM_MODEL if job.transcription_provider == "deepgram" else settings.OPENAI_TRANSCRIPTION_MODEL
    if not job.transcription_language:
        job.transcription_language = settings.DEEPGRAM_LANGUAGE if job.transcription_provider == "deepgram" else settings.TRANSCRIPTION_LANGUAGE
    job.verification_model = job.verification_model or settings.OPENAI_VERIFICATION_MODEL
    job.save(update_fields=["transcription_provider", "transcription_model", "transcription_language", "verification_model"])


def usage_dict(usage):
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return usage if isinstance(usage, dict) else vars(usage)


class TranscriptionProvider(ABC):
    @abstractmethod
    def transcribe(self, file_path, known_speakers=None): ...


class OpenAITranscriptionProvider(TranscriptionProvider):
    def __init__(self, model=None, language=None):
        self.model = model or settings.OPENAI_TRANSCRIPTION_MODEL
        self.language = language or settings.TRANSCRIPTION_LANGUAGE

    def transcribe(self, file_path, known_speakers=None):
        from openai import OpenAI
        try:
            with open(file_path, "rb") as audio:
                response = OpenAI(api_key=settings.OPENAI_API_KEY).audio.transcriptions.create(
                    model=self.model, file=audio, language=self.language,
                    response_format="diarized_json", chunking_strategy="auto",
                    extra_body=known_speakers or {},
                )
        except Exception:
            raise ProviderError("Falha na transcrição OpenAI.") from None
        return {
            "segments": [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in response.segments],
            "usage": usage_dict(getattr(response, "usage", None)),
            "duration": getattr(response, "duration", None),
        }


class OpenAIVerificationProvider:
    def __init__(self, model=None):
        self.model = model or settings.OPENAI_VERIFICATION_MODEL

    def transcribe(self, file_path):
        from openai import OpenAI
        try:
            with open(file_path, "rb") as audio:
                response = OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=0, timeout=120).audio.transcriptions.create(
                    model=self.model, file=audio, response_format="json",
                    prompt=("Gravação de conversa em português brasileiro. Transcrição literal no idioma falado, "
                            "sem tradução, resumo ou complementação. Preserve números, repetições e frases incompletas."),
                    extra_body={"languages": ["pt"]},
                )
        except Exception:
            raise ProviderError("Falha na verificação OpenAI.") from None
        return {"text": response.text, "usage": usage_dict(getattr(response, "usage", None))}


class DeepgramTranscriptionProvider(TranscriptionProvider):
    def __init__(self, model=None, language=None):
        self.model = model or settings.DEEPGRAM_MODEL
        self.language = language or settings.DEEPGRAM_LANGUAGE

    def transcribe(self, file_path, known_speakers=None):
        try:
            with open(file_path, "rb") as audio:
                response = requests.post(
                    "https://api.deepgram.com/v1/listen", data=audio,
                    headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}", "Content-Type": "audio/wav"},
                    params={"model": self.model, "language": self.language, "diarize_model": "v2",
                            "utterances": "true", "punctuate": "true"},
                    timeout=(15, 900),
                )
            response.raise_for_status()
            payload = response.json()
            results = payload["results"]
            utterances = results.get("utterances") or []
            segments = []
            for utterance in utterances:
                # Speaker 0 is valid. Never test it with a truthiness check.
                label = utterance.get("speaker")
                words = utterance.get("words") or []
                if label is None and words:
                    labels = {w.get("speaker") for w in words if w.get("speaker") is not None}
                    if len(labels) == 1:
                        label = next(iter(labels))
                segments.append({"start": utterance["start"], "end": utterance["end"],
                                 "text": utterance.get("transcript", ""),
                                 "speaker": str(label) if label is not None else None,
                                 "provider_label": str(label) if label is not None else "",
                                 "speaker_scope": "global" if label is not None else "unknown",
                                 "words": words})
            # Some responses contain only word-level results. Group contiguous
            # words by speaker, preserving both timestamps and original words.
            if not segments:
                words = results["channels"][0]["alternatives"][0].get("words", [])
                for word in words:
                    label = word.get("speaker")
                    label = str(label) if label is not None else None
                    if not segments or label != segments[-1]["speaker"] or word["start"] - segments[-1]["end"] > 1:
                        segments.append({"start": word["start"], "end": word["end"], "text": "", "speaker": label,
                                         "provider_label": label if label is not None else "",
                                         "speaker_scope": "global" if label is not None else "unknown", "words": []})
                    item = segments[-1]
                    item["end"] = word["end"]
                    item["words"].append(word)
                    item["text"] = (item["text"] + " " + word.get("punctuated_word", word.get("word", ""))).strip()
                # Preserve text even if the provider omitted word timings.
                alternative = results.get("channels", [{}])[0].get("alternatives", [{}])[0]
                if not segments and alternative.get("transcript"):
                    segments.append({"start": 0, "end": payload["metadata"]["duration"],
                                     "text": alternative["transcript"], "speaker": None,
                                     "review_reasons": ["invalid_segment"]})
            return {"segments": segments, "usage": {"seconds": payload["metadata"]["duration"]},
                    "duration": payload["metadata"]["duration"]}
        except (requests.RequestException, KeyError, TypeError, ValueError, IndexError):
            raise ProviderError("Falha na transcrição Deepgram.") from None
