(() => {
  'use strict';

  document.addEventListener('submit', event => {
    const form = event.target.closest('form[data-loading]');
    if (!form) return;
    if (form.dataset.submitting === 'true') {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = 'true';
    form.setAttribute('aria-busy', 'true');
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (button) {
      button.disabled = true;
      const spinner = document.createElement('span');
      spinner.className = 'spinner';
      spinner.setAttribute('aria-hidden', 'true');
      button.replaceChildren(spinner, document.createTextNode(form.dataset.loading));
    }
  });

  async function request(url, json = true) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: {Accept: json ? 'application/json' : 'text/html'},
      });
      if (response.redirected || response.status === 401 || response.status === 403) {
        throw Object.assign(new Error('session'), {terminal: true});
      }
      if (response.status === 404) throw Object.assign(new Error('deleted'), {terminal: true});
      if (!response.ok) throw new Error('connection');
      return json ? await response.json() : await response.text();
    } finally {
      clearTimeout(timeout);
    }
  }

  function poll(container, update) {
    let delay = 3000;
    const message = container.querySelector('[data-poll-message]');
    async function tick() {
      let again = true;
      try {
        again = await update();
        delay = 3000;
        message.textContent = '';
      } catch (error) {
        if (error.terminal) {
          message.textContent = error.message === 'session'
            ? 'Sua sessão expirou. Entre novamente para acompanhar o pedido.'
            : 'Este pedido não está mais disponível.';
          return;
        }
        message.textContent = 'Reconectando… Seu pedido continua sendo processado.';
        delay = Math.min(delay * 2, 30000);
      }
      if (again) setTimeout(tick, delay);
    }
    setTimeout(tick, delay);
  }

  const detail = document.querySelector('[data-job-poll]');
  if (detail && detail.querySelector('#job-content').dataset.busy === 'true') {
    poll(detail, async () => {
      const state = await request(detail.dataset.statusUrl);
      const current = detail.querySelector('#job-content');
      if (state.version !== current.dataset.version) {
        const html = await request(detail.dataset.detailUrl + '?partial=1', false);
        const template = document.createElement('template');
        template.innerHTML = html.trim();
        const replacement = template.content.querySelector('#job-content');
        if (!replacement) throw new Error('fragment');
        current.replaceWith(replacement);
      }
      return detail.querySelector('#job-content').dataset.busy === 'true';
    });
  }

  const dashboard = document.querySelector('[data-dashboard-poll]');
  if (dashboard && dashboard.querySelector('[data-job-id][data-busy="true"]')) {
    poll(dashboard, async () => {
      const {jobs} = await request(dashboard.dataset.statusUrl);
      const states = new Map(jobs.map(job => [job.id, job]));
      dashboard.querySelectorAll('[data-job-id]').forEach(row => {
        const state = states.get(row.dataset.jobId);
        if (!state) { row.remove(); return; }
        row.dataset.busy = String(state.busy);
        document.dispatchEvent(new CustomEvent('job-title-updated', {detail: state}));
        const badge = row.querySelector('.status');
        badge.textContent = state.label;
        badge.className = 'status status-' + state.status;
      });
      return Boolean(dashboard.querySelector('[data-job-id][data-busy="true"]'));
    });
  }
})();
