(() => {
  'use strict';
  const dialog = document.querySelector('[data-rename-dialog]');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const form = dialog.querySelector('form');
  const input = form.querySelector('[name=title]');
  const error = form.querySelector('[data-rename-error]');
  const submit = form.querySelector('[type=submit]');
  let jobId = null, opener = null, busy = false, feedbackTimer;
  function updateTitles(state) {
    document.querySelectorAll('[data-job-title]').forEach(element => {
      if (element.dataset.jobTitle !== state.id) return;
      element.textContent = state.title;
      if (element.tagName === 'H1') document.title = state.title + ' · Voz em Texto';
    });
    document.querySelectorAll('[data-rename-job]').forEach(element => {
      if (element.dataset.renameJob !== state.id) return;
      element.dataset.currentTitle = state.title;
      if (element.hasAttribute('aria-label')) element.setAttribute('aria-label', 'Renomear pedido ' + state.title);
    });
    document.querySelectorAll('[data-original-filename-for]').forEach(element => {
      if (element.dataset.originalFilenameFor === state.id) element.hidden = state.title === state.original_filename;
    });
  }
  document.addEventListener('job-title-updated', event => updateTitles(event.detail));
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-rename-job]');
    if (!link || busy) return;
    event.preventDefault();
    opener = link;
    jobId = link.dataset.renameJob;
    form.action = link.href;
    form.querySelector('[name=return_to]').value = link.dataset.returnTo || 'detail';
    input.value = link.dataset.currentTitle;
    error.textContent = '';
    dialog.showModal();
    input.focus(); input.select();
  });
  dialog.querySelector('[data-cancel-rename]').addEventListener('click', () => { if (!busy) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { if (opener?.isConnected) opener.focus(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const title = input.value.trim();
    if (!title) { error.textContent = 'Informe um nome para o pedido.'; input.focus(); return; }
    busy = true; submit.disabled = true; input.readOnly = true; error.textContent = '';
    try {
      const data = new FormData(form); data.set('title', title);
      const response = await fetch(form.action, {method: 'POST', credentials: 'same-origin', headers: {Accept: 'application/json'}, body: data});
      if (response.redirected || response.status === 401 || response.status === 403) throw new Error('Sua sessão expirou. Entre novamente para renomear.');
      if (response.status === 404) throw new Error('Este pedido não está mais disponível.');
      const result = await response.json();
      if (!response.ok) throw new Error(result.errors?.title?.[0]?.message || 'Não foi possível salvar o nome. Tente novamente.');
      if (result.id !== jobId) throw new Error('Não foi possível confirmar a alteração.');
      updateTitles(result);
      dialog.close();
      const feedback = document.querySelector('[data-rename-feedback]');
      feedback.textContent = 'Pedido renomeado.'; feedback.hidden = false;
      clearTimeout(feedbackTimer); feedbackTimer = setTimeout(() => { feedback.hidden = true; }, 4000);
    } catch (failure) {
      error.textContent = failure.message || 'Não foi possível salvar o nome. Tente novamente.';
    } finally {
      busy = false; submit.disabled = false; input.readOnly = false;
    }
  });
})();
