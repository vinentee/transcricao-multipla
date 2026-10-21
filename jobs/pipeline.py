"""Whole-file diarization and a separately budgeted text verification pass."""
import os
import re

from django.conf import settings

from .audio import AudioChunk, SpeakerReferences, chunks_for_audio, cut_pcm, prepare_audio
from .media import build_playback
from .providers import DeepgramTranscriptionProvider, OpenAITranscriptionProvider, OpenAIVerificationProvider, usage_dict
from .quality import assess, collect_issues, is_portuguese, merge_chunks


def usage_entry(provider, model, result, seconds, role="primary"):
    usage = usage_dict(result.get("usage"))
    return {"provider": provider, "model": model, "role": role,
            "audio_seconds": usage.get("seconds", seconds),
            "reported": bool(usage), "input_tokens": usage.get("input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0}


def verify_segments(segments, prepared, model):
    budget, spent, attempts, usages = prepared.duration * .2, 0, 0, []
    candidates = {"english", "repetition", "empty_text", "missing_speech"}
    for index, item in sorted(enumerate(segments), key=lambda pair: (pair[1]["start"], pair[1]["end"])):
        reasons = set(item.get("review_reasons", []))
        if not reasons & candidates:
            continue
        duration = item["end"] - item["start"]
        if duration <= 0 or "invalid_segment" in reasons:
            continue
        if not settings.OPENAI_API_KEY:
            item["review_reasons"] = sorted(reasons | {"verification_unavailable"})
            continue
        if spent + duration > budget + .00001:
            item["review_reasons"] = sorted(reasons | {"verification_budget"})
            continue
        spent += duration
        attempts += 1
        path = os.path.join(os.path.dirname(prepared.path), f"verification-{index:05}.wav")
        try:
            cut_pcm(prepared.path, path, item["start"], duration)
            result = OpenAIVerificationProvider(model).transcribe(path)
            usages.append(usage_entry("openai", model, result, duration, role="verification"))
            text = str(result.get("text") or "").strip()
            candidate = {"start": 0, "end": duration, "text": text}
            _, issues, _ = assess([candidate], duration, [], "pt")
            overlap = any(other is not item and max(item["start"], other["start"]) < min(item["end"], other["end"]) for other in segments)
            known_speaker = item.get("speaker") is not None
            # A text-only pass cannot relabel overlapping voices or prove which
            # numbers are right if the two recognizers disagree.
            original_numbers = re.findall(r"\d+(?:[.,]\d+)*", item["text"])
            replacement_numbers = re.findall(r"\d+(?:[.,]\d+)*", text)
            safe_numbers = not original_numbers or original_numbers == replacement_numbers
            apply = bool(text and is_portuguese(text) and not issues and "english" in reasons
                         and known_speaker and not overlap and safe_numbers)
            item.setdefault("alternatives", []).append({"text": text, "provider": "openai", "model": model, "applied": apply})
            if apply:
                item["raw_text"] = item["text"]
                item["text"] = item["corrected_text"] = text
                reasons -= {"english", "repetition", "empty_text"}
            else:
                reasons.add("verification_unresolved")
            item["review_reasons"] = sorted(reasons)
        except Exception:
            item["review_reasons"] = sorted(reasons | {"verification_failed"})
        finally:
            if os.path.exists(path):
                os.unlink(path)
    return {"attempts": attempts, "seconds": round(spent, 6), "budget_seconds": round(budget, 6)}, usages


def transcribe_prepared(job, prepared, playback=True, verify=True):
    provider = job.transcription_provider or "openai"  # Legacy jobs/quotes.
    model = job.transcription_model or (settings.DEEPGRAM_MODEL if provider == "deepgram" else settings.OPENAI_TRANSCRIPTION_MODEL)
    language = job.transcription_language or (settings.DEEPGRAM_LANGUAGE if provider == "deepgram" else settings.TRANSCRIPTION_LANGUAGE)
    usages = []
    if playback:
        build_playback(job, prepared.path)
    if provider == "deepgram":
        result = DeepgramTranscriptionProvider(model, language).transcribe(prepared.path)
        segments, issues, _ = assess(result["segments"], prepared.duration, prepared.speech, language)
        usages.append(usage_entry(provider, model, result, prepared.duration))
        count = 1
    else:
        results = []
        client = OpenAITranscriptionProvider(model, language)
        with SpeakerReferences() as references:
            for chunk in chunks_for_audio(prepared):
                payload = references.payload()
                result = client.transcribe(chunk.path, known_speakers=payload) if payload else client.transcribe(chunk.path)
                local, _, _ = assess(result["segments"], chunk.duration, chunk.speech, language)
                references.identify(local, chunk)
                results.append((chunk.offset, chunk.duration, local))
                usages.append(usage_entry(provider, model, result, chunk.duration))
        segments = merge_chunks(results)
        segments, issues, _ = assess(segments, prepared.duration, prepared.speech, language)
        count = len(results)
    # Gaps have their own review cards and audio controls, even without text.
    for issue in issues:
        if issue["code"] == "missing_speech":
            segments.append({"start": issue["start"], "end": issue["end"], "text": "", "speaker": None,
                             "speaker_scope": "unknown", "review_reasons": ["missing_speech", "unknown_speaker"]})
    for item in segments:
        if item.get("speaker") is None:
            item["review_reasons"] = sorted(set(item.get("review_reasons", [])) | {"unknown_speaker"})
    stats = {"attempts": 0, "seconds": 0, "budget_seconds": prepared.duration * .2}
    if verify:
        stats, extra = verify_segments(segments, prepared, job.verification_model or settings.OPENAI_VERIFICATION_MODEL)
        usages.extend(extra)
    segments.sort(key=lambda item: (item["start"], item["end"]))
    return {"segments": segments, "issues": collect_issues(segments), "usage": usages,
            "metadata": {"version": 2, "provider": provider, "model": model, "language": language,
                         "chunks": count, "verification": stats}}


def transcribe_with_quality(job):
    with prepare_audio(job) as prepared:
        return transcribe_prepared(job, prepared)
