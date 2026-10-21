"""Speaker turns and validated manual transcript structure changes."""
from copy import deepcopy
import json
import re

from django.db.models import Max
from django.utils import timezone

from .models import TranscriptSegment


def group_speaker_turns(segments):
    """Group only confirmed, non-overlapping identities after quality checks."""
    ordered = sorted(deepcopy(segments), key=lambda item: (item['start'], item['end']))
    # Any intersection is a barrier, including a long voice crossing later turns.
    ambiguous = set()
    active = []
    for index, item in enumerate(ordered):
        active = [(i, end) for i, end in active if end > item['start']]
        if active:
            ambiguous.add(index)
            ambiguous.update(i for i, _ in active)
        active.append((index, item['end']))
    turns = []
    last_index = None
    for index, item in enumerate(ordered):
        source = deepcopy(item)
        label = item.get('speaker')
        scope = item.get('speaker_scope', 'global' if label is not None else 'unknown')
        join = (turns and label is not None and scope == 'global'
                and turns[-1].get('speaker_scope', 'global') == 'global'
                and str(turns[-1].get('speaker')) == str(label)
                and index not in ambiguous and last_index not in ambiguous
                and turns[-1]['end'] <= item['start'])
        if not join:
            item['source_segments'] = [source]
            turns.append(item)
        else:
            turn = turns[-1]
            turn['source_segments'].append(source)
            # Preserve each initial and corrected version in provenance; only the
            # displayed text is concatenated across a speaker's complete turn.
            effective = lambda s: s.get('corrected_text') or s.get('text', '')
            turn['text'] = ' '.join(effective(s).strip() for s in turn['source_segments'] if effective(s).strip())
            turn['raw_text'] = ' '.join(s.get('raw_text', s.get('text', '')).strip() for s in turn['source_segments'] if s.get('raw_text', s.get('text', '')).strip())
            turn['corrected_text'] = turn['text'] if any(s.get('corrected_text') for s in turn['source_segments']) else ''
            turn['words'] = [word for s in turn['source_segments'] for word in s.get('words', [])]
            turn['alternatives'] = []  # Alternatives remain attached to source intervals.
            turn['review_reasons'] = sorted({code for s in turn['source_segments'] for code in s.get('review_reasons', [])})
            turn['needs_review'] = any(s.get('needs_review') or s.get('review_reasons') for s in turn['source_segments'])
            turn['end'] = item['end']
        last_index = index
    return turns


class StructureError(ValueError):
    pass


def validate_structure(job, post, allowed_speakers):
    """Validate the complete batch before the caller mutates any database rows."""
    try:
        changes = json.loads(post.get('structure_changes', '{}'))
    except (TypeError, ValueError):
        raise StructureError('As alterações de estrutura são inválidas.') from None
    if not isinstance(changes, dict) or set(changes) - {'replacements', 'additions'}:
        raise StructureError('As alterações de estrutura são inválidas.')
    replacements, additions = changes.get('replacements', []), changes.get('additions', [])
    if not isinstance(replacements, list) or not isinstance(additions, list) or len(replacements) + len(additions) > 1000:
        raise StructureError('Quantidade de alterações inválida.')
    if not replacements and not additions:
        return [], []
    if 'review_version' not in post:
        raise StructureError('Atualize a página antes de alterar a estrutura.')
    duration = (job.duration_seconds or 0) * 1000
    current = {str(s.pk): s for s in job.segments.filter(superseded_at__isnull=True, excluded_at__isnull=True)}
    seen_sources, seen_ids = set(), set()

    def part(value):
        if not isinstance(value, dict):
            raise StructureError('Trecho inválido.')
        start, end = value.get('start_ms'), value.get('end_ms')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration:
            raise StructureError('Confira os horários: cada trecho deve ter duração positiva e estar dentro do áudio.')
        text, speaker, token = value.get('text'), value.get('speaker', ''), value.get('id', '')
        if not isinstance(text, str) or len(text) > 500000 or not isinstance(speaker, str) or (speaker and speaker not in allowed_speakers):
            raise StructureError('Texto ou falante inválido para este pedido.')
        if not isinstance(token, str) or not re.fullmatch(r'draft_[a-f0-9-]{36}', token) or token in seen_ids:
            raise StructureError('Identificador de trecho inválido ou repetido.')
        seen_ids.add(token)
        if type(value.get('deleted', False)) is not bool or type(value.get('reviewed', False)) is not bool:
            raise StructureError('Estado do trecho inválido.')
        return dict(value, start_ms=start, end_ms=end, text=text, speaker=speaker)

    normalized = []
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise StructureError('Divisão inválida.')
        source_id = replacement.get('source_id')
        if not isinstance(source_id, str) or source_id not in current or source_id in seen_sources:
            raise StructureError('O trecho dividido não está mais disponível.')
        seen_sources.add(source_id)
        source = current[source_id]
        if post.get(f'delete_segment_{source.pk}') == '1':
            raise StructureError('Não é possível excluir e dividir o mesmo trecho.')
        values = replacement.get('parts')
        if not isinstance(values, list) or not 2 <= len(values) <= 100:
            raise StructureError('A divisão deve conter pelo menos duas partes.')
        parts = [part(value) for value in values]
        if parts[0]['start_ms'] != source.start_ms or parts[-1]['end_ms'] != source.end_ms or any(a['end_ms'] != b['start_ms'] for a, b in zip(parts, parts[1:])):
            raise StructureError('As partes devem cobrir todo o trecho original, sem lacunas ou sobreposição.')
        normalized.append((source, parts))
    new = [part(value) for value in additions]
    if any(not value['text'].strip() for value in new if not value.get('deleted')):
        raise StructureError('Escreva o texto do novo trecho manual.')
    ranges = [(s.start_ms, s.end_ms) for sid, s in current.items() if sid not in seen_sources and post.get(f'delete_segment_{sid}') != '1']
    ranges += [(p['start_ms'], p['end_ms']) for _, parts in normalized for p in parts if not p.get('deleted')]
    # Restored segments also participate in overlap validation.
    ranges += [(s.start_ms, s.end_ms) for s in job.segments.filter(superseded_at__isnull=True, excluded_at__isnull=False) if post.get(f'restore_segment_{s.pk}') == '1']
    for item in new:
        if item.get('deleted'):
            continue
        others = ranges + [(other['start_ms'], other['end_ms']) for other in new if other is not item and not other.get('deleted')]
        if any(max(item['start_ms'], start) < min(item['end_ms'], end) for start, end in others) and item.get('overlap_confirmed') is not True:
            raise StructureError('Confirme a sobreposição do novo trecho com as falas existentes.')
    return normalized, new


def affected_reasons(source, start, end):
    located = {reason for item in source.source_segments for reason in item.get('review_reasons', [])}
    reasons = set(source.review_reasons) - located
    for item in source.source_segments:
        if max(start, round(item.get('start', source.start_ms / 1000) * 1000)) < min(end, round(item.get('end', source.end_ms / 1000) * 1000)):
            reasons.update(set(item.get('review_reasons', [])) & set(source.review_reasons))
    return reasons


def apply_structure(job, replacements, additions, speakers, confirm_all=False):
    now = timezone.now()
    ordinal = job.segments.aggregate(value=Max('ordinal'))['value'] or 0
    for source, parts in replacements:
        # Refresh after ordinary text/speaker edits in the same transaction.
        source.refresh_from_db()
        source.superseded_at = now
        source.save(update_fields=['superseded_at'])
        for item in parts:
            ordinal += 1
            reasons = affected_reasons(source, item['start_ms'], item['end_ms'])
            speaker = speakers.get(item['speaker'])
            reasons -= {'unknown_speaker', 'speaker_local'} if speaker else set()
            if not speaker:
                reasons.add('unknown_speaker')
            if source.needs_review and not source.review_reasons:
                reasons.add('manual_check')
            reviewed = confirm_all or item.get('reviewed', False)
            TranscriptSegment.objects.create(job=job, ordinal=ordinal, parent_segment=source,
                source_segments=[{'start': max(issue['start'], item['start_ms'] / 1000), 'end': min(issue['end'], item['end_ms'] / 1000), 'review_reasons': issue['review_reasons']} for issue in segment_issues(source) if max(issue['start'], item['start_ms'] / 1000) < min(issue['end'], item['end_ms'] / 1000)],
                origin='split', start_ms=item['start_ms'], end_ms=item['end_ms'], raw_text='',
                revised_text=item['text'], has_manual_revision=True, speaker=speaker,
                speaker_scope='global' if speaker else 'unknown',
                provider_words=[word for word in source.provider_words if word.get('start', -1) * 1000 >= item['start_ms'] and word.get('end', float('inf')) * 1000 <= item['end_ms']],
                needs_review=bool(reasons) and not reviewed, review_reasons=[] if reviewed else sorted(reasons),
                reviewed_at=now if reviewed else None, excluded_at=now if item.get('deleted') else None)
    for item in additions:
        if item.get('deleted'):
            continue
        ordinal += 1
        reviewed = confirm_all or item.get('reviewed', False)
        speaker = speakers.get(item['speaker'])
        reasons = ['manual_check'] + ([] if speaker else ['unknown_speaker'])
        TranscriptSegment.objects.create(job=job, ordinal=ordinal, origin='manual',
            start_ms=item['start_ms'], end_ms=item['end_ms'], raw_text='', revised_text=item['text'],
            has_manual_revision=True, speaker=speaker, speaker_scope='global' if speaker else 'unknown',
            needs_review=not reviewed, review_reasons=[] if reviewed else reasons, reviewed_at=now if reviewed else None)
    if replacements or additions:
        # Reserve a disjoint range first to avoid the unique (job, ordinal) constraint.
        rows = list(job.segments.order_by('start_ms', 'end_ms', 'ordinal'))
        offset = (job.segments.aggregate(value=Max('ordinal'))['value'] or 0) + len(rows) + 1
        for index, row in enumerate(rows):
            row.ordinal = offset + index
        TranscriptSegment.objects.bulk_update(rows, ['ordinal'])
        for index, row in enumerate(rows, 1):
            row.ordinal = index
        TranscriptSegment.objects.bulk_update(rows, ['ordinal'])


def segment_issues(segment):
    """Retain original issue intervals even when a turn spans many utterances."""
    reasons = set(segment.review_reasons)
    items = []
    for source in segment.source_segments:
        local = reasons & set(source.get('review_reasons', []))
        if local:
            items.append({'start': source['start'], 'end': source['end'], 'review_reasons': sorted(local)})
    located = {code for item in items for code in item['review_reasons']}
    if reasons - located:
        items.append({'start': segment.start_ms / 1000, 'end': segment.end_ms / 1000, 'review_reasons': sorted(reasons - located)})
    return items
