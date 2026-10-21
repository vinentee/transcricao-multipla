"""An explicit, paid benchmark; never changes an AudioJob or a user's review."""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.management.base import BaseCommand, CommandError

from jobs.audio import prepare_audio
from jobs.pipeline import transcribe_prepared
from jobs.providers import require_provider


class Command(BaseCommand):
    help = "Compara Deepgram e OpenAI em um arquivo local. Faz chamadas pagas e salva relatório privado, sem alterar pedidos."

    def add_arguments(self, parser):
        parser.add_argument("--audio", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--providers", default="deepgram,openai")

    def handle(self, *args, **options):
        source = Path(options["audio"]).resolve()
        output = Path(options["output"]).resolve()
        providers = list(dict.fromkeys(options["providers"].split(",")))
        if not source.is_file() or not output.parent.is_dir() or output.exists():
            raise CommandError("Informe um áudio existente e um relatório novo em diretório existente.")
        for provider in providers:
            try:
                require_provider(provider)
            except ValueError as exc:
                raise CommandError(str(exc)) from None
        # Reserve the output before any paid call; refuse overwrites and symlinks.
        descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        report = {"audio": source.name, "results": {}}
        try:
            base = SimpleNamespace(original_filename=source.name, original_key=source.name)
            with prepare_audio(base, storage=FileSystemStorage(location=source.parent)) as prepared:
                report["duration_seconds"] = prepared.duration
                for provider in providers:
                    job = SimpleNamespace(transcription_provider=provider,
                                          transcription_model=settings.DEEPGRAM_MODEL if provider == "deepgram" else settings.OPENAI_TRANSCRIPTION_MODEL,
                                          transcription_language=settings.DEEPGRAM_LANGUAGE if provider == "deepgram" else settings.TRANSCRIPTION_LANGUAGE,
                                          verification_model=settings.OPENAI_VERIFICATION_MODEL)
                    started = time.monotonic()
                    try:
                        result = transcribe_prepared(job, prepared, playback=False, verify=False)
                        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                        report["results"][provider] = result
                    except Exception:
                        report["results"][provider] = {"error": "Falha no provedor; consulte sua configuração.", "elapsed_seconds": round(time.monotonic() - started, 3)}
        finally:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False)
        self.stdout.write(self.style.SUCCESS("Comparação concluída. Relatório privado salvo no caminho informado."))
