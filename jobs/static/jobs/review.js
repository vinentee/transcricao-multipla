(() => {
  'use strict';
  function initialize(root) {
    if (root.dataset.initialized) return;
    root.dataset.initialized = 'true';
    const form = root.querySelector('[data-review-form]');
    const audio = root.querySelector('[data-review-audio]');
    const status = root.querySelector('[data-media-message]');
    const cards = [...root.querySelectorAll('[data-review-segment]')];
    const filter = root.querySelector('[data-review-filter]');
    const speakerFilter = root.querySelector('[data-speaker-filter]');
    const speed = root.querySelector('[data-playback-speed]');
    const repeat = root.querySelector('[data-repeat]');
    let dirty = false, range = null, currentCard = null, nextIndex = -1, playbackRequest = 0;
    let draftSpeakerCount = 0;
    const legacyPending = Math.max(0, Number(root.dataset.pendingTotal) - cards.filter(card => card.dataset.pending === 'true').length);
    let polling = false, sourceAt = 0, recovering = false, playTimer = null;

    function resize(input) {
      input.style.height = 'auto';
      input.style.height = Math.max(110, Math.min(480, input.scrollHeight + 2)) + 'px';
    }
    cards.forEach(card => { card.dataset.originalPending = card.dataset.pending; });
    function resizeMany(inputs) {
      inputs.forEach(input => { input.style.height = 'auto'; });
      const heights = inputs.map(input => Math.max(110, Math.min(480, input.scrollHeight + 2)));
      inputs.forEach((input, index) => { input.style.height = heights[index] + 'px'; });
    }
    function changed() {
      dirty = true;
      root.querySelector('[data-save-state]').textContent = 'Alterações não salvas.';
    }
    function applyFilters() {
      let visible = 0;
      const textareas = [];
      cards.forEach(card => {
        const deleting = card.dataset.deleting === 'true';
        card.hidden = !deleting && ((filter.value === 'pending' && card.dataset.pending !== 'true') ||
          (speakerFilter.value !== 'all' && speakerFilter.value !== card.dataset.speaker));
        if (!card.hidden && !deleting) { visible++; textareas.push(card.querySelector('[data-transcript]')); }
        const badge = card.querySelector('.review-badge');
        if (badge) badge.textContent = card.dataset.pending === 'true' ? 'Revisão pendente' : 'Conferido · não salvo';
      });
      resizeMany(textareas);
      root.querySelector('[data-no-matches]').hidden = visible > 0;
      const pending = (form.querySelector('[name="confirm_quality_review"]')?.checked ? 0 : legacyPending) + cards.filter(card => card.dataset.pending === 'true' && card.dataset.deleting !== 'true').length;
      root.querySelector('[data-pending-summary]').textContent = `${pending} trecho${pending === 1 ? '' : 's'} para revisar`;
    }
    filter.addEventListener('change', applyFilters);
    speakerFilter.addEventListener('change', applyFilters);
    form.addEventListener('input', event => {
      if (!event.target.name) return;
      changed();
      if (event.target.matches('[data-transcript]')) resize(event.target);
    });
    form.addEventListener('change', event => {
      const card = event.target.closest('[data-review-segment]');
      if (event.target.name === 'confirm_quality_review') {
        cards.forEach(item => {
          const reviewed = item.querySelector('[data-reviewed]');
          item.dataset.pending = event.target.checked || reviewed?.checked ? 'false' : item.dataset.originalPending;
        });
        applyFilters();
      }
      if (event.target.matches('[data-reviewed]')) {
        card.dataset.pending = event.target.checked ? 'false' : card.dataset.originalPending;
        applyFilters();
      }
      if (event.target.name?.startsWith('assign_')) {
        card.dataset.speaker = event.target.value || 'unknown';
        applyFilters();
      }
    });
    // Submit only changed text/assignments. Long transcripts otherwise exceed
    // Django's form field limit and unnecessarily rewrite every segment.
    const initialValues = new Map([...form.elements].filter(element => element.name && element.type !== 'checkbox' && element.type !== 'hidden').map(element => [element.name, element.value]));
    form.addEventListener('formdata', event => {
      for (const [name, value] of [...event.formData.entries()]) {
        if (initialValues.has(name) && initialValues.get(name) === value) event.formData.delete(name);
      }
    });
    form.addEventListener('submit', () => { dirty = false; });
    window.addEventListener('beforeunload', event => {
      if (dirty && root.isConnected) { event.preventDefault(); event.returnValue = ''; }
    });
    root.querySelector('[data-next-pending]').addEventListener('click', () => {
      let target = null;
      for (let step = 1; step <= cards.length; step++) {
        const index = (nextIndex + step) % cards.length;
        const card = cards[index];
        if (card.dataset.deleting !== 'true' && card.dataset.pending === 'true' && (speakerFilter.value === 'all' || speakerFilter.value === card.dataset.speaker)) {
          nextIndex = index; target = card; break;
        }
      }
      if (target) {
        applyFilters();
        target.scrollIntoView({behavior: 'smooth', block: 'start'});
        target.querySelector('textarea').focus({preventScroll: true});
      } else {
        root.querySelector('[data-save-state]').textContent = 'Nenhuma pendência para o falante selecionado.';
      }
    });
    root.addEventListener('click', event => {
      const remove = event.target.closest('[data-delete-segment]');
      const undo = event.target.closest('[data-undo-delete]');
      if (!remove && !undo) return;
      const card = (remove || undo).closest('[data-review-segment]');
      const deleting = Boolean(remove);
      card.dataset.deleting = String(deleting);
      card.classList.toggle('is-deleting', deleting);
      card.querySelector('[data-delete-marker]').disabled = !deleting;
      card.querySelector('[data-segment-body]').hidden = deleting;
      card.querySelector('[data-deletion-notice]').hidden = !deleting;
      if (deleting) playbackRequest++;
      if (deleting && currentCard === card) {
        audio.pause(); range = null;
        card.classList.remove('is-playing'); currentCard = null;
      }
      changed(); applyFilters();
      card.querySelector(deleting ? '[data-undo-delete]' : '[data-delete-segment]').focus();
    });
    root.querySelector('[data-add-speaker]').addEventListener('click', () => {
      const input = root.querySelector('[data-new-speaker-name]');
      const name = input.value.trim();
      if (!name || draftSpeakerCount >= 20) { input.focus(); return; }
      const token = 'new_' + (++draftSpeakerCount);
      const hidden = document.createElement('input');
      hidden.type = 'hidden'; hidden.name = 'new_speaker_' + token; hidden.value = name;
      root.querySelector('[data-new-speaker-inputs]').append(hidden);
      root.querySelectorAll('[name^="assign_"], [data-speaker-filter]').forEach(select => {
        select.add(new Option(name, token));
      });
      input.value = ''; changed();
    });
    root.addEventListener('click', event => {
      const use = event.target.closest('[data-use-alternative]');
      if (!use) return;
      const textarea = use.closest('[data-review-segment]').querySelector('[data-transcript]');
      textarea.value = use.closest('.alternative').querySelector('[data-alternative-text]').textContent;
      resize(textarea); changed(); textarea.focus();
    });

    async function mediaState() {
      const response = await fetch(root.dataset.mediaStatusUrl, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok || response.redirected) throw new Error('Áudio indisponível ou sessão expirada.');
      return response.json();
    }
    async function loadSource(force = false) {
      const state = await mediaState();
      root.dataset.mediaStatus = state.status;
      if (state.status === 'ready' && state.url) {
        if (!audio.src || force || Date.now() - sourceAt > 240000) {
          audio.src = state.url + (state.url.includes('?') ? '&' : '?') + 'v=' + Date.now();
          sourceAt = Date.now();
        }
        root.querySelector('[data-prepare-audio]').textContent = 'Atualizar áudio';
        return true;
      }
      return false;
    }
    async function prepare() {
      if (await loadSource()) { status.textContent = 'Áudio pronto para reprodução.'; return true; }
      if (root.dataset.mediaStatus !== 'preparing') {
        const response = await fetch(root.dataset.mediaPrepareUrl, {
          method: 'POST', credentials: 'same-origin',
          headers: {'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value},
        });
        if (!response.ok || response.redirected) throw new Error('Não foi possível preparar o áudio.');
      }
      status.textContent = 'Preparando áudio para reprodução…';
      pollMedia();
      return false;
    }
    async function pollMedia() {
      if (polling || !root.isConnected) return;
      polling = true;
      try {
        if (await loadSource()) {
          status.textContent = 'Áudio pronto. Clique em “Ouvir trecho” para começar.';
          polling = false; return;
        }
        if (root.dataset.mediaStatus === 'failed') {
          status.textContent = 'A preparação falhou. Clique em “Preparar áudio” para tentar novamente.';
          polling = false; return;
        }
      } catch (error) {
        status.textContent = error.message;
        polling = false; return;
      }
      setTimeout(() => { polling = false; pollMedia(); }, 3000);
    }
    function waitMetadata() {
      if (audio.readyState >= 1) return Promise.resolve();
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { cleanup(); reject(new Error('O áudio demorou para carregar. Tente novamente.')); }, 15000);
        function cleanup() { clearTimeout(timer); audio.removeEventListener('loadedmetadata', loaded); audio.removeEventListener('error', failed); }
        function loaded() { cleanup(); resolve(); }
        function failed() { cleanup(); reject(new Error('Não foi possível carregar o áudio. Atualize e tente novamente.')); }
        audio.addEventListener('loadedmetadata', loaded, {once: true});
        audio.addEventListener('error', failed, {once: true});
        audio.load();
      });
    }
    async function playCard(card) {
      const ticket = ++playbackRequest;
      audio.pause();
      if (!await prepare() || ticket !== playbackRequest || card.dataset.deleting === 'true') return;
      await waitMetadata();
      if (ticket !== playbackRequest || card.dataset.deleting === 'true') return;
      const duration = Number.isFinite(audio.duration) ? audio.duration : Number(root.dataset.duration);
      range = {start: Math.max(0, Number(card.dataset.start) / 1000 - 1), end: Math.min(duration, Number(card.dataset.end) / 1000 + 1)};
      if (range.end <= range.start) throw new Error('O horário deste trecho não corresponde ao áudio disponível.');
      if (currentCard) currentCard.classList.remove('is-playing');
      currentCard = card; currentCard.classList.add('is-playing');
      audio.currentTime = range.start;
      audio.playbackRate = Number(speed.value);
      await audio.play();
      status.textContent = 'Reproduzindo ' + card.querySelector('.segment-time').textContent + ' com contexto.';
    }
    root.querySelector('[data-prepare-audio]').addEventListener('click', async () => {
      try { await prepare(); } catch (error) { status.textContent = error.message; }
    });
    root.addEventListener('click', async event => {
      const button = event.target.closest('[data-listen]');
      if (!button) return;
      try { await playCard(button.closest('[data-review-segment]')); }
      catch (error) { status.textContent = error.message || 'Clique novamente para reproduzir.'; }
    });
    function checkBoundary() {
      if (range && !audio.paused && audio.currentTime >= range.end) {
        if (repeat.checked) { audio.currentTime = range.start; }
        else { audio.pause(); if (currentCard) currentCard.classList.remove('is-playing'); }
      }
    }
    audio.addEventListener('play', () => { clearInterval(playTimer); playTimer = setInterval(checkBoundary, 50); });
    audio.addEventListener('pause', () => clearInterval(playTimer));
    audio.addEventListener('ended', () => {
      clearInterval(playTimer);
      if (range && repeat.checked) { audio.currentTime = range.start; audio.play().catch(() => {}); }
    });
    audio.addEventListener('seeking', () => {
      if (range && (audio.currentTime < range.start - .5 || audio.currentTime > range.end + .5)) {
        range = null;
        if (currentCard) currentCard.classList.remove('is-playing');
      }
    });
    audio.addEventListener('timeupdate', checkBoundary);
    speed.addEventListener('change', () => { audio.playbackRate = Number(speed.value); });
    audio.addEventListener('error', async () => {
      if (!audio.src || recovering) return;
      recovering = true;
      const position = audio.currentTime;
      try {
        if (await loadSource(true)) {
          await waitMetadata(); audio.currentTime = position;
          status.textContent = 'Acesso ao áudio renovado. Clique para continuar a reprodução.';
        }
      } catch (error) { status.textContent = error.message; }
      setTimeout(() => { recovering = false; }, 5000);
    });
    if (root.dataset.mediaStatus === 'preparing') pollMedia();
    applyFilters();
  }
  function init() { document.querySelectorAll('[data-review-editor]').forEach(initialize); }
  init();
  new MutationObserver(records => {
    if (records.some(record => [...record.addedNodes].some(node => node.nodeType === 1 && (node.id === 'job-content' || node.matches?.('[data-review-editor]'))))) init();
  }).observe(document.body, {childList: true, subtree: true});
})();
