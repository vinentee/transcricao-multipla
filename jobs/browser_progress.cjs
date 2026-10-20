// Browser verification uses Django-rendered fixtures and intercepts all HTTP requests.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = require(process.argv[2]);

(async () => {
  const fixtures = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.clock.install();
    let state = 'validating';
    let pageHtml = fixtures.initial;
    let disconnected = false;
    let statusCalls = 0;
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/static/jobs/')) {
        const filename = url.pathname.split('/').pop();
        return route.fulfill({contentType: filename.endsWith('.js') ? 'text/javascript' : 'text/css', body: fs.readFileSync(fixtures.root + '/jobs/static/jobs/' + filename, 'utf8')});
      }
      if (url.pathname === fixtures.statusUrl || url.pathname === '/pedidos/status/') {
        statusCalls++;
        if (disconnected) return route.abort();
        const body = url.pathname === fixtures.statusUrl ? fixtures.fixtures[state].status : {jobs: [fixtures.fixtures[state].status]};
        return route.fulfill({json: body});
      }
      return route.fulfill({contentType: 'text/html', body: url.searchParams.has('partial') ? fixtures.fixtures[state].fragment : pageHtml});
    });
    await page.goto('http://progress.test' + fixtures.detailUrl);
    await page.clock.fastForward(3000);
    await page.waitForFunction(() => document.querySelector('#job-content').dataset.busy === 'true');
    disconnected = true;
    await page.clock.fastForward(3000);
    await page.getByText('Reconectando… Seu pedido continua sendo processado.').waitFor();
    disconnected = false;
    state = 'quoted';
    await page.clock.fastForward(6000);
    await page.getByRole('button', {name: 'Confirmar pagamento de teste'}).waitFor();
    // Preserve native POST behavior but prevent navigation in this fixture-only test.
    await page.evaluate(() => document.addEventListener('submit', event => event.preventDefault()));
    await page.getByRole('button', {name: 'Confirmar pagamento de teste'}).click();
    assert.equal(await page.getByRole('button', {name: 'Confirmando pagamento…'}).isDisabled(), true);

    state = 'paid_queued';
    pageHtml = fixtures.fixtures.paid_queued.page;
    await page.goto('http://progress.test' + fixtures.detailUrl);
    await page.getByText('Seu áudio está na fila').waitFor();
    state = 'processing';
    await page.clock.fastForward(3000);
    await page.getByText('Preparando sua transcrição').waitFor();
    state = 'ready';
    await page.clock.fastForward(3000);
    await page.getByRole('heading', {name: 'Revise sua transcrição'}).waitFor();
    await page.getByRole('link', {name: 'PDF', exact: true}).waitFor();
    await page.locator('textarea').fill('Revisão preservada');
    const completedCalls = statusCalls;
    await page.clock.fastForward(30000);
    assert.equal(statusCalls, completedCalls);
    assert.equal(await page.locator('textarea').inputValue(), 'Revisão preservada');

    state = 'validating';
    pageHtml = fixtures.initial;
    await page.goto('http://progress.test' + fixtures.detailUrl);
    state = 'failed';
    await page.clock.fastForward(3000);
    await page.getByText('Não foi possível concluir a transcrição.').waitFor();
    const failedCalls = statusCalls;
    await page.clock.fastForward(30000);
    assert.equal(statusCalls, failedCalls);

    state = 'processing';
    pageHtml = fixtures.dashboard;
    await page.goto('http://progress.test/');
    await page.clock.fastForward(3000);
    await page.locator('.order-row[data-busy="true"] .status-processing').waitFor();
    state = 'ready';
    await page.clock.fastForward(3000);
    await page.locator('.order-row[data-busy="false"] .status-ready').waitFor();
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: quote, queue, processing, ready, failure, reconnection, buttons, dashboard and edit preservation.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
