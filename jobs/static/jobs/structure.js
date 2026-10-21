(() => {
  'use strict';
  const clock = ms => {
    if (!Number.isFinite(ms)) return '';
    ms = Math.round(ms);
    const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60, s = Math.floor(ms / 1000) % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
  };
  const parse = value => {
    const match = /^(\d{2,}):([0-5]\d):([0-5]\d)(?:[.,](\d{1,3}))?$/.exec(value.trim());
    return match ? Number(match[1]) * 3600000 + Number(match[2]) * 60000 + Number(match[3]) * 1000 + Number((match[4] || '').padEnd(3, '0')) : NaN;
  };
  function initialize(root) {
    if (!root.reviewEditor || root.dataset.structureInitialized) return;
    root.dataset.structureInitialized = 'true';
    const api = root.reviewEditor, form = root.querySelector('[data-review-form]');
    const audio = root.querySelector('audio'), panel = root.querySelector('[data-structure-panel]');
    const container = root.querySelector('.review-segments'), partsContainer = panel.querySelector('[data-structure-parts]');
    panel.addEventListener('input', () => api.changed());
    const error = panel.querySelector('[data-structure-error]'), undo = root.querySelector('[data-undo-structure]');
    const duration = Number(root.dataset.duration) * 1000;
    const alignment = JSON.parse(root.querySelector('#review-alignment')?.textContent || '{}');
    const sources = new Map(), history = [];
    let editing = null, selectedParts = [];
    const cards = () => [...container.querySelectorAll('[data-review-segment]')];
    const field = (row, name) => row.querySelector(`[data-part-${name}]`);
    function speakers(select, value = '') {
      select.replaceChildren(...[...root.querySelector('[data-speaker-filter]').options].filter(o => o.value !== 'all').map(o => new Option(o.value === 'unknown' ? 'Falante não identificado' : o.textContent, o.value === 'unknown' ? '' : o.value)));
      select.value = value;
    }
    function showError(message, success = false) { error.textContent = message; error.dataset.success = String(success); }
    function insert(card) {
      const next = cards().find(other => Number(other.dataset.start) > Number(card.dataset.start) || (Number(other.dataset.start) === Number(card.dataset.start) && Number(other.dataset.end) > Number(card.dataset.end)));
      container.insertBefore(card, next || null);
    }
    function suggestions(text, positions, words) {
      // No inferred character-to-time proportions, fuzzy matches or stale alignment.
      const tokens = [...text.matchAll(/\S+/gu)];
      if (!words?.length || words.length !== tokens.length || words.some((w, i) => (w.punctuated_word || w.word) !== tokens[i][0] || !Number.isFinite(w.start) || !Number.isFinite(w.end))) return positions.map(() => null);
      return positions.map(position => {
        const index = tokens.findIndex(token => token.index >= position);
        if (index <= 0 || tokens[index - 1].index + tokens[index - 1][0].length > position || text.slice(tokens[index - 1].index + tokens[index - 1][0].length, position).trim()) return null;
        return Math.round(words[index].start * 1000);
      });
    }
    function readParts() {
      return [...partsContainer.children].map(row => ({start_ms: parse(field(row, 'start').value), end_ms: parse(field(row, 'end').value), text: field(row, 'text').value, speaker: field(row, 'speaker').value}));
    }
    function conflicts(part) {
      if (editing?.card || !Number.isFinite(part?.start_ms) || !Number.isFinite(part?.end_ms)) return [];
      const active = cards().filter(card => card.dataset.deleting !== 'true');
      const restored = [...form.querySelectorAll('[name^="restore_segment_"]:checked')].map(input => ({dataset: {start: input.dataset.start, end: input.dataset.end}}));
      return [...active, ...restored].filter(card => Math.max(part.start_ms, Number(card.dataset.start)) < Math.min(part.end_ms, Number(card.dataset.end)));
    }
    function overlapMessage() {
      const overlapping = conflicts(readParts()[0]);
      panel.querySelector('[data-overlap-warning]').hidden = !overlapping.length;
      panel.querySelector('[data-overlap-text]').textContent = 'Este intervalo se sobrepõe a: ' + overlapping.map(card => `${clock(Number(card.dataset.start))} – ${clock(Number(card.dataset.end))}`).join('; ');
    }
    function render() {
      partsContainer.replaceChildren();
      selectedParts.forEach((part, index) => {
        const row = document.createElement('div'); row.className = 'structure-part';
        row.innerHTML = '<h4></h4><label>Falante<select data-part-speaker aria-label="Falante da parte"></select></label><div class="structure-times"><div class="structure-time"><label>Início<input type="text" data-part-start placeholder="00:00:00.000" aria-label="Início da parte"></label><button type="button" class="button secondary" data-position="start">Usar posição atual como início</button></div><div class="structure-time"><label>Fim<input type="text" data-part-end placeholder="00:00:00.000" aria-label="Fim da parte"></label><button type="button" class="button secondary" data-position="end">Usar posição atual como fim</button></div></div><button type="button" class="button secondary" data-preview-part>▶ Ouvir esta parte</button><label>Texto<textarea data-part-text rows="3" aria-label="Texto da parte"></textarea></label>';
        row.querySelector('h4').textContent = editing.card ? `Parte ${index + 1}` : 'Novo trecho';
        speakers(field(row, 'speaker'), part.speaker);
        field(row, 'start').value = clock(part.start_ms);
        field(row, 'end').value = clock(part.end_ms);
        field(row, 'text').value = part.text;
        if (editing.card && index === 0) { field(row, 'start').readOnly = true; row.querySelector('[data-position=start]').disabled = true; }
        if (editing.card && index === selectedParts.length - 1) { field(row, 'end').readOnly = true; row.querySelector('[data-position=end]').disabled = true; }
        partsContainer.append(row);
      });
      panel.querySelector('[data-confirm-cuts]').checked = false;
      panel.querySelector('[data-confirm-overlap]').checked = false;
      panel.hidden = false; showError(''); overlapMessage();
      panel.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
    function close() { api.stop(); panel.hidden = true; editing = null; }
    root.addEventListener('click', event => {
      const split = event.target.closest('[data-split]');
      if (!split) return;
      if (editing) { showError('Aplique ou cancele a alteração aberta antes de iniciar outra.'); panel.scrollIntoView(); return; }
      const card = split.closest('[data-review-segment]'), input = card.querySelector('[data-transcript]');
      const text = input.value, start = input.selectionStart, end = input.selectionEnd;
      const positions = [...new Set([start, end].filter(position => position > 0 && position < text.length))];
      if (!positions.length && text.length) {
        root.querySelector('[data-save-state]').textContent = 'Posicione o cursor no ponto de corte ou selecione as palavras que deseja separar.';
        input.focus(); return;
      }
      const points = suggestions(text, positions, alignment[card.dataset.segmentId]);
      const boundaries = [0, ...positions, text.length];
      selectedParts = boundaries.slice(0, -1).map((position, i) => ({text: text.slice(position, boundaries[i + 1]), speaker: card.querySelector('select[name^=assign_]').value,
        start_ms: i === 0 ? Number(card.dataset.start) : points[i - 1],
        end_ms: i === positions.length ? Number(card.dataset.end) : points[i]}));
      if (!text.length) selectedParts = [{text: '', speaker: card.dataset.speaker === 'unknown' ? '' : card.dataset.speaker, start_ms: Number(card.dataset.start), end_ms: null}, {text: '', speaker: card.dataset.speaker === 'unknown' ? '' : card.dataset.speaker, start_ms: null, end_ms: Number(card.dataset.end)}];
      editing = {card};
      panel.querySelector('[data-structure-heading]').textContent = start !== end ? 'Separar seleção' : 'Dividir parágrafo';
      panel.querySelector('[data-structure-help]').textContent = points.some(point => point === null) || !text ? 'O texto não possui alinhamento confiável para estes cortes. Ouça o áudio e marque os horários; nenhum horário será estimado pelo tamanho do texto.' : 'Horários sugeridos pelas palavras da transcrição. Confira cada corte ouvindo o áudio antes de aplicar.';
      render();
    });
    root.querySelector('[data-add-manual]').addEventListener('click', () => {
      if (editing) { showError('Aplique ou cancele a alteração aberta antes de iniciar outra.'); panel.scrollIntoView(); return; }
      editing = {card: null};
      const start = Math.min(Math.max(0, Math.round(audio.currentTime * 1000) || 0), Math.max(0, duration - 1));
      selectedParts = [{start_ms: start, end_ms: Math.min(duration, start + 5000), text: '', speaker: ''}];
      panel.querySelector('[data-structure-heading]').textContent = 'Novo trecho manual';
      panel.querySelector('[data-structure-help]').textContent = 'Marque início e fim, ouça o intervalo e escreva a fala que falta.';
      render();
    });
    function syncBoundary(row, name) {
      const rows = [...partsContainer.children], index = rows.indexOf(row);
      if (editing.card) {
        if (name === 'start' && index > 0) field(rows[index - 1], 'end').value = field(row, name).value;
        if (name === 'end' && index < rows.length - 1) field(rows[index + 1], 'start').value = field(row, name).value;
      }
      panel.querySelector('[data-confirm-cuts]').checked = false;
      panel.querySelector('[data-confirm-overlap]').checked = false;
      overlapMessage();
    }
    partsContainer.addEventListener('input', event => {
      const name = event.target.matches('[data-part-start]') ? 'start' : event.target.matches('[data-part-end]') ? 'end' : null;
      if (name) syncBoundary(event.target.closest('.structure-part'), name);
    });
    partsContainer.addEventListener('click', async event => {
      const row = event.target.closest('.structure-part');
      if (!row) return;
      const position = event.target.closest('[data-position]');
      if (position) {
        field(row, position.dataset.position).value = clock(Math.min(duration, Math.round(audio.currentTime * 1000)));
        syncBoundary(row, position.dataset.position);
      }
      if (event.target.closest('[data-preview-part]')) {
        const start = parse(field(row, 'start').value), end = parse(field(row, 'end').value);
        if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || end > duration) { showError('Informe início e fim válidos para ouvir esta parte.'); return; }
        try { await api.playRange(start, end); showError(''); } catch (failure) { showError(failure.message); }
      }
    });
    panel.querySelector('[data-cancel-structure]').addEventListener('click', close);
    panel.querySelector('[data-structure-add-speaker]').addEventListener('click', () => {
      const input = panel.querySelector('[data-structure-speaker-name]'), name = input.value.trim();
      if (!name) { showError('Informe o nome do novo falante.'); return; }
      const existingInput = root.querySelector('[data-new-speaker-name]'); existingInput.value = name;
      root.querySelector('[data-add-speaker]').click();
      const option = [...root.querySelector('[data-speaker-filter]').options].at(-1);
      if (option?.textContent !== name) { showError('Não foi possível adicionar outro falante. Limite de 20 novos falantes por revisão.'); return; }
      partsContainer.querySelectorAll('[data-part-speaker]').forEach(select => { const previous = select.value; speakers(select, previous); });
      input.value = ''; showError('Falante adicionado. Selecione-o na parte desejada.', true);
    });
    function draftCard(part, source) {
      const card = document.createElement('article');
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
      const id = 'draft_' + [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
      card.className = 'review-segment'; card.id = 'segment-' + id;
      card.dataset.reviewSegment = ''; card.dataset.segmentId = id; card.dataset.draft = 'true';
      card.dataset.rootSource = source?.dataset.rootSource || (source?.dataset.draft ? '' : source?.dataset.segmentId) || '';
      card.dataset.start = part.start_ms; card.dataset.end = part.end_ms;
      card.dataset.speaker = part.speaker || 'unknown';
      card.dataset.pending = source ? source.dataset.originalPending || source.dataset.pending : 'true';
      card.dataset.overlapConfirmed = String(part.overlap_confirmed || source?.dataset.overlapConfirmed === 'true');
      card.innerHTML = '<input type="hidden" value="1" data-delete-marker disabled><div class="deletion-notice" data-deletion-notice hidden role="status"><span>Este trecho será excluído ao salvar.</span><button type="button" class="button secondary" data-undo-delete>Desfazer</button></div><div data-segment-body><p class="draft-label">Alteração estrutural · não salva</p><header class="segment-header"><label class="segment-speaker"><span class="sr-only">Falante deste trecho</span><select aria-label="Falante deste trecho"></select></label><span class="segment-time"></span><button type="button" class="button secondary listen-button" data-listen>▶ Ouvir trecho</button><span class="review-badge">Revisão pendente</span><button type="button" class="button secondary split-segment" data-split>Dividir</button><button type="button" class="delete-segment" data-delete-segment>Excluir trecho</button></header><label class="sr-only">Texto do trecho</label><textarea data-transcript rows="3"></textarea><div class="segment-review-actions"><p>Confira as partes com o áudio. As versões originais serão preservadas no histórico.</p><label class="quality-confirm compact-check"><input type="checkbox" value="1" data-reviewed> Trecho conferido</label></div></div>';
      card.querySelector('select').name = 'assign_' + id;
      speakers(card.querySelector('select'), part.speaker);
      const input = card.querySelector('textarea'); input.name = 'draft_text_' + id; input.value = part.text;
      input.id = 'text-' + id; card.querySelector('label.sr-only').htmlFor = input.id;
      card.querySelector('[data-delete-marker]').name = 'delete_segment_' + id;
      card.querySelector('[data-reviewed]').name = 'reviewed_' + id;
      card.querySelector('.segment-time').textContent = `${clock(part.start_ms)} – ${clock(part.end_ms)}`;
      return card;
    }
    panel.querySelector('[data-apply-structure]').addEventListener('click', () => {
      const parts = readParts();
      if (parts.some(p => !Number.isFinite(p.start_ms) || !Number.isFinite(p.end_ms) || !(0 <= p.start_ms && p.start_ms < p.end_ms && p.end_ms <= duration))) { showError('Use horários válidos (hh:mm:ss.mmm), com duração positiva e dentro do áudio.'); return; }
      if (editing.card && (parts[0].start_ms !== Number(editing.card.dataset.start) || parts.at(-1).end_ms !== Number(editing.card.dataset.end) || parts.some((p, i) => i > 0 && parts[i - 1].end_ms !== p.start_ms))) { showError('As partes devem cobrir o trecho original inteiro, sem lacunas ou sobreposição.'); return; }
      if (!editing.card && !parts[0].text.trim()) { showError('Escreva o texto do novo trecho.'); return; }
      if (!panel.querySelector('[data-confirm-cuts]').checked) { showError('Confirme os horários após conferir o áudio.'); return; }
      const overlapping = conflicts(parts[0]);
      if (overlapping.length && !panel.querySelector('[data-confirm-overlap]').checked) { overlapMessage(); showError('Confirme a sobreposição com os trechos indicados.'); return; }
      if (overlapping.length) {
        parts[0].overlap_confirmed = true;
        overlapping.filter(card => card.dataset.draft === 'true').forEach(card => { card.dataset.overlapConfirmed = 'true'; });
      }
      const source = editing.card;
      const sourceNew = source && source.dataset.draft !== 'true';
      if (sourceNew) sources.set(source.dataset.segmentId, source);
      const added = parts.map(part => draftCard(part, source));
      if (source) source.remove();
      added.forEach(insert);
      history.push({source, added, sourceNew});
      undo.hidden = false;
      close(); api.refresh(); added[0].scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    undo.addEventListener('click', () => {
      if (editing) { showError('Cancele ou aplique a edição aberta antes de desfazer.'); return; }
      const operation = history.pop();
      if (!operation) return;
      api.stop(); operation.added.forEach(card => card.remove());
      if (operation.source) insert(operation.source);
      if (operation.sourceNew) sources.delete(operation.source.dataset.segmentId);
      undo.hidden = !history.length; api.refresh();
    });
    form.addEventListener('formdata', event => {
      const drafts = cards().filter(card => card.dataset.draft === 'true');
      const serialize = card => ({id: card.dataset.segmentId, start_ms: Number(card.dataset.start), end_ms: Number(card.dataset.end), text: card.querySelector('textarea').value,
        speaker: card.querySelector('select').value, deleted: card.dataset.deleting === 'true', reviewed: card.querySelector('[data-reviewed]').checked, overlap_confirmed: card.dataset.overlapConfirmed === 'true'});
      const replacements = [...sources].map(([id, source]) => {
        // Keep the user's current source revision in the audit record, too.
        event.formData.set('segment_' + id, source.querySelector('textarea').value);
        event.formData.set('assign_' + id, source.querySelector('select').value);
        return {source_id: id, parts: drafts.filter(card => card.dataset.rootSource === id).map(serialize)};
      });
      for (const name of [...event.formData.keys()]) {
        if (/^(?:draft_text_|assign_draft_|reviewed_draft_|delete_segment_draft_)/.test(name)) event.formData.delete(name);
      }
      event.formData.set('structure_changes', JSON.stringify({replacements, additions: drafts.filter(card => !card.dataset.rootSource).map(serialize)}));
    });
  }
  const init = () => document.querySelectorAll('[data-review-editor]').forEach(initialize);
  init();
  new MutationObserver(init).observe(document.body, {childList: true, subtree: true});
})();
