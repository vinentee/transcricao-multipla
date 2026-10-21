"""Conservative transcript checks: flags are evidence for review, not proof of error."""
import math
import re
from langdetect import DetectorFactory, LangDetectException, detect_langs

DetectorFactory.seed = 0

REVIEW_LABELS = {
    "manual_check": "Confira o trecho editado manualmente com o áudio.",
    "speaker_local": "Falante distinguido neste bloco; associação entre blocos pendente",
    "verification_failed": "Não foi possível verificar novamente este trecho",
    "verification_unavailable": "Verificação adicional indisponível",
    "verification_budget": "Limite de verificação adicional atingido",
    "verification_unresolved": "Confira a alternativa de transcrição com o áudio",
    "english": "Possível mudança para inglês",
    "repetition": "Repetição incomum",
    "missing_speech": "Voz sem transcrição correspondente",
    "invalid_segment": "Segmento com horário inválido",
    "empty_text": "Segmento sem texto",
    "boundary_conflict": "Transcrições divergentes na junção dos blocos",
    "unknown_speaker": "Identidade do falante não confirmada",
    "retry_failed": "Não foi possível verificar novamente este trecho",
    "retry_unresolved": "Dúvida mantida após nova tentativa",
}


def words(text):
    return re.findall(r"[^\W\d_]+", text.casefold(), flags=re.UNICODE)


def normalized(text):
    return " ".join(re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE))


def interval_length(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def uncovered_speech(speech, segments):
    coverage = sorted((s["start"], s["end"]) for s in segments if s.get("text", "").strip() and "invalid_segment" not in s.get("review_reasons", []))
    gaps = []
    for start, end in speech:
        cursor = start
        for left, right in coverage:
            if right <= cursor or left >= end:
                continue
            if left > cursor:
                gaps.append((cursor, min(left, end)))
            cursor = max(cursor, min(right, end))
        if cursor < end:
            gaps.append((cursor, end))
    return gaps


def assess(segments, duration, speech, language="pt"):
    clean, issues = [], []
    for raw in segments:
        item = dict(raw)
        reasons = set(item.get("review_reasons", []))
        try:
            start, end = float(item["start"]), float(item["end"])
            if not all(map(math.isfinite, (start, end))) or end <= start or end <= 0 or start >= duration:
                raise ValueError("invalid timestamps")
            if start < 0 or end > duration + 0.1:
                reasons.add("invalid_segment")
            item["start"], item["end"] = max(0, start), min(duration, end)
        except (ValueError, TypeError, KeyError):
            # Keep recoverable text rather than silently throwing it away.
            item["start"], item["end"] = 0, duration
            reasons.add("invalid_segment")
        item["text"] = str(item.get("text") or "")
        tokens = words(item["text"])
        if not item["text"].strip():
            reasons.add("empty_text")
        for i in range(max(0, len(tokens) - 15)):
            phrase = tokens[i:i + 4]
            if all(tokens[i + j * 4:i + (j + 1) * 4] == phrase for j in range(1, 4)):
                reasons.add("repetition")
                break
        item["review_reasons"] = sorted(reasons)
        clean.append(item)
    short_english = {"thank you", "thanks", "my god", "i don't know", "i dont know", "yes", "exactly", "you know"}
    if language.split("-")[0].lower() == "pt":
        for item in clean:
            text = " ".join(words(item["text"]))
            if len(words(text)) < 20 and any(re.search(r"\b" + re.escape(phrase) + r"\b", text) for phrase in short_english):
                item["review_reasons"] = sorted(set(item["review_reasons"]) | {"english"})
    # Sliding passages catch short English interventions without classifying names.
    text_words = [(token, index) for index, s in enumerate(clean) for token in words(s["text"])]
    if language.split("-")[0].lower() == "pt":
        for offset in range(0, max(0, len(text_words) - 19), 10):
            passage = text_words[offset:offset + 40]
            try:
                english = any(lang.lang == "en" and lang.prob >= .9 for lang in detect_langs(" ".join(w for w, _ in passage)))
            except LangDetectException:
                english = False
            if english:
                for index in {index for _, index in passage}:
                    clean[index]["review_reasons"] = sorted(set(clean[index]["review_reasons"]) | {"english"})
    for item in clean:
        issues.extend({"code": code, "start": item["start"], "end": item["end"]} for code in item["review_reasons"])
    gaps = uncovered_speech(speech, clean)
    issues.extend({"code": "missing_speech", "start": start, "end": end} for start, end in gaps if end - start >= 1.5)
    return clean, issues, interval_length(gaps)


def choose_retry(original, retry):
    """A retry must reduce flagged problems without losing VAD coverage."""
    def score(result):
        return len(result[1])
    return retry if score(retry) < score(original) and retry[2] <= original[2] + .01 else original


def merge_chunks(chunks):
    """Only exact text matches at matching times in adjacent overlaps are deduped."""
    result, previous = [], None
    for offset, duration, segments in chunks:
        current = []
        for raw in segments:
            item = dict(raw)
            item["review_reasons"] = list(raw.get("review_reasons", []))
            item["start"], item["end"] = raw["start"] + offset, raw["end"] + offset
            duplicate = False
            if previous:
                prev_offset, prev_end, prev_segments = previous
                overlap_start, overlap_end = max(offset, prev_offset), min(offset + duration, prev_end)
                for old in prev_segments:
                    if not (max(item["start"], old["start"], overlap_start) < min(item["end"], old["end"], overlap_end)):
                        continue
                    shared = min(item["end"], old["end"]) - max(item["start"], old["start"])
                    extent = max(item["end"], old["end"]) - min(item["start"], old["start"])
                    near = abs(item["start"] - old["start"]) <= .5 and abs(item["end"] - old["end"]) <= .5 and shared / extent >= .8
                    same_voice = item.get("speaker") is not None and item.get("speaker") == old.get("speaker")
                    # Without a confirmed voice, simultaneous identical interjections
                    # may be different people: keep and flag rather than delete.
                    if near and normalized(item["text"]) and normalized(item["text"]) == normalized(old["text"]) and same_voice:
                        old["review_reasons"] = sorted(set(old["review_reasons"] + item["review_reasons"]))
                        duplicate = True
                        break
                    for segment in (old, item):
                        segment["review_reasons"] = sorted(set(segment["review_reasons"]) | {"boundary_conflict"})
            if not duplicate:
                current.append(item)
                result.append(item)
            else:
                # Retain boundary evidence for the next neighbor, even if deduped.
                current.append(old)
        previous = (offset, offset + duration, current)
    return sorted(result, key=lambda s: (s["start"], s["end"]))


def clock(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def is_portuguese(text):
    tokens = words(text)
    if not tokens:
        return False
    short = {"sim", "não", "obrigado", "obrigada", "certo", "exatamente", "meu", "deus", "entendi", "claro", "pois", "é", "isso", "tá", "bom", "muito", "bem"}
    if len(tokens) <= 3 and all(token in short for token in tokens):
        return True
    try:
        return any(item.lang == "pt" and item.prob >= .9 for item in detect_langs(text))
    except LangDetectException:
        return False


def collect_issues(segments, gaps=()):
    issues = list(gaps)
    for item in segments:
        issues.extend({"code": code, "start": item["start"], "end": item["end"]} for code in item.get("review_reasons", []))
    return [dict(code=code, start=start, end=end) for code, start, end in sorted({(i["code"], i["start"], i["end"]) for i in issues}, key=lambda i: (i[1], i[2], i[0]))]
