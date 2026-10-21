"""Temporary PCM preparation, voice activity analysis and speaker references."""
import base64
import math
import os
import subprocess
import tempfile
import wave
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage


@dataclass
class AudioChunk:
    path: str
    offset: float
    duration: float
    speech: list = field(default_factory=list)
    source: str = ""


def voice_intervals(path):
    import webrtcvad
    vad = webrtcvad.Vad(1)  # Conservative: do not use VAD to remove audio.
    intervals = []
    with wave.open(path, "rb") as audio:
        frame_samples = 480  # 30 ms at 16 kHz
        position = 0
        while True:
            frame = audio.readframes(frame_samples)
            if not frame:
                break
            duration = len(frame) / 32000
            padded = frame.ljust(frame_samples * 2, b"\0")
            if vad.is_speech(padded, 16000):
                start, end = position / 16000, position / 16000 + duration
                if intervals and start - intervals[-1][1] < .3:
                    intervals[-1][1] = end
                else:
                    intervals.append([start, end])
            position += len(frame) // 2
    return intervals


def boundaries(duration, speech, size, overlap):
    if not all(math.isfinite(v) for v in (duration, size, overlap)) or duration <= 0 or size <= 10 or not 0 <= overlap < size - 10:
        raise ValueError("Duração ou configuração de blocos inválida.")
    pauses, cursor = [], 0
    for start, end in speech:
        if start - cursor >= .3:
            pauses.append((cursor, start))
        cursor = end
    if duration - cursor >= .3:
        pauses.append((cursor, duration))
    start = 0
    while start < duration:
        end = min(duration, start + size)
        if end < duration:
            candidates = [min(right, end) for left, right in pauses if max(left, end - 10) < min(right, end)]
            if candidates:
                end = max(candidates)
        if end <= start or (end < duration and end - overlap <= start):
            raise ValueError("O corte do áudio não avançou.")
        yield start, end
        if end >= duration:
            break
        start = end - overlap


def cut_pcm(source, output, start, duration):
    with wave.open(source, "rb") as audio, wave.open(output, "wb") as target:
        target.setparams(audio.getparams())
        audio.setpos(round(start * audio.getframerate()))
        remaining = round(duration * audio.getframerate())
        while remaining > 0:
            frames = audio.readframes(min(remaining, 16000 * 30))
            if not frames:
                break
            target.writeframes(frames)
            remaining -= len(frames) // (audio.getsampwidth() * audio.getnchannels())
    if not 44 < os.path.getsize(output) < 25_000_000:
        raise ValueError("Bloco vazio ou superior ao limite de 25 MB.")


@contextmanager
def prepare_chunks(job):
    with tempfile.TemporaryDirectory(prefix="transcription-") as workdir:
        source = os.path.join(workdir, "original" + Path(job.original_filename).suffix.lower())
        with default_storage.open(job.original_key, "rb") as remote, open(source, "wb") as local:
            for data in iter(lambda: remote.read(1024 * 1024), b""):
                local.write(data)
        pcm = os.path.join(workdir, "normalized.wav")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", source, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", pcm], capture_output=True, timeout=600, check=True)
        with wave.open(pcm, "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        if not math.isfinite(duration) or duration <= 0 or duration > settings.MAX_AUDIO_SECONDS + 1:
            raise ValueError("Duração do áudio inválida ou superior ao limite.")
        speech = voice_intervals(pcm)
        chunks = []
        for index, (start, end) in enumerate(boundaries(duration, speech, settings.TRANSCRIPTION_CHUNK_SECONDS, settings.TRANSCRIPTION_OVERLAP_SECONDS)):
            path = os.path.join(workdir, f"chunk-{index:04}.wav")
            cut_pcm(pcm, path, start, end - start)
            local_speech = [(max(left, start) - start, min(right, end) - start) for left, right in speech if right > start and left < end]
            chunks.append(AudioChunk(path, start, end - start, local_speech, pcm))
        yield chunks


class SpeakerReferences:
    def __init__(self):
        self.references = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.references.clear()

    def payload(self):
        return {"known_speaker_names": list(self.references), "known_speaker_references": list(self.references.values())} if self.references else {}

    def identify(self, segments, chunk, allow_new=True):
        """Only references sent on this request can identify existing people."""
        known = set(self.references)
        mapping = {name: name for name in known}
        if allow_new and chunk.source:
            for label in dict.fromkeys(s.get("speaker") for s in segments):
                if not label or label in mapping or len(self.references) >= 4:
                    continue
                for item in segments:
                    if item.get("speaker") != label or item.get("review_reasons") or item["end"] - item["start"] < 2:
                        continue
                    start, end = item["start"], min(item["end"], item["start"] + 10)
                    if any(other is not item and max(start, other["start"]) < min(end, other["end"]) for other in segments):
                        continue
                    voiced = sum(max(0, min(end, b) - max(start, a)) for a, b in chunk.speech)
                    if voiced < 1.5:
                        continue
                    name = f"voice_{len(self.references) + 1}"
                    # Collision with a local provider label must never imply identity.
                    if label in self.references:
                        continue
                    path = os.path.join(os.path.dirname(chunk.path), f"{name}.wav")
                    cut_pcm(chunk.source, path, chunk.offset + start, end - start)
                    with open(path, "rb") as audio:
                        self.references[name] = "data:audio/wav;base64," + base64.b64encode(audio.read()).decode("ascii")
                    os.unlink(path)
                    mapping[label] = name
                    break
        for item in segments:
            label = item.get("speaker")
            item["speaker"] = mapping.get(label)
            if item["speaker"] is None:
                item["review_reasons"] = sorted(set(item.get("review_reasons", [])) | {"unknown_speaker"})
