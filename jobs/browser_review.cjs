const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = require(process.argv[2]);
(async () => {
  const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    let mediaState = 'ready', mediaRequests = 0, prepareRequests = 0, renameRequests = 0;
    const audioBytes = Buffer.from(fixture.audio, 'base64');
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.startsWith('/static/jobs/')) {
        const name = url.pathname.split('/').pop();
        return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : 'text/css', body: fs.readFileSync(fixture.root + '/jobs/static/jobs/' + name)});
      }
      if (url.pathname === fixture.renameUrl) {
        renameRequests++;
        if (renameRequests === 1) return route.fulfill({status: 400, json: {errors: {title: [{message: 'Nome inválido para o teste.'}]}}});
        assert.ok(request.postData().includes('Reunião revisada'));
        return route.fulfill({json: {id: fixture.jobId, title: 'Reunião revisada', original_filename: fixture.originalFilename}});
      }
      if (url.pathname === fixture.mediaStatusUrl) return route.fulfill({json: {status: mediaState, url: mediaState === 'ready' ? fixture.mediaUrl : null}});
      if (url.pathname === fixture.prepareUrl) { prepareRequests++; mediaState = 'preparing'; return route.fulfill({status: 202, json: {status: mediaState}}); }
      if (url.pathname === fixture.mediaUrl) {
        mediaRequests++;
        const range = request.headers()['range'];
        if (range) {
          const parts = /bytes=(\d+)-(\d*)/.exec(range);
          const start = Number(parts[1]), end = parts[2] ? Math.min(Number(parts[2]), audioBytes.length - 1) : audioBytes.length - 1;
          return route.fulfill({status: 206, headers: {'Content-Type': 'audio/mpeg', 'Accept-Ranges': 'bytes', 'Content-Range': `bytes ${start}-${end}/${audioBytes.length}`, 'Content-Length': String(end - start + 1)}, body: audioBytes.subarray(start, end + 1)});
        }
        return route.fulfill({contentType: 'audio/mpeg', headers: {'Accept-Ranges': 'bytes'}, body: audioBytes});
      }
      return route.fulfill({contentType: 'text/html', body: fixture.html});
    });
    await page.goto('http://review.test' + fixture.detailUrl);
    await page.waitForFunction(() => document.querySelector('[data-review-editor]').dataset.initialized === 'true');
    const cards = page.locator('[data-review-segment]');
    const first = cards.nth(0), second = cards.nth(1);
    assert.equal(await cards.count(), 30);
    assert.equal(await page.locator('.segment-reasons[open]').count(), 0);
    assert.equal(await page.locator('.order-details[open]').count(), 0);
    const textBox = await first.locator('textarea').boundingBox();
    const cardBox = await first.boundingBox();
    assert.ok(textBox.width > cardBox.width * .9, 'transcript must occupy full card width');
    assert.ok(textBox.width > 800, 'desktop text is not in a narrow metadata column');
    await page.screenshot({path: '/tmp/transcription-review-desktop.png', fullPage: false});
    const edited = 'Texto editado que deve ser preservado ao mudar os filtros. '.repeat(15);
    await first.locator('textarea').fill(edited);
    assert.ok((await first.locator('textarea').boundingBox()).height > 110);
    await page.locator('[data-rename-job]').click();
    await page.locator('[data-rename-dialog] [name=title]').fill('Reunião revisada');
    await page.locator('[data-rename-dialog] [type=submit]').click();
    await page.getByText('Nome inválido para o teste.').waitFor();
    assert.equal(await first.locator('textarea').inputValue(), edited);
    await page.locator('[data-rename-dialog] [type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('[data-rename-dialog]').open);
    assert.equal(await page.locator('h1[data-job-title]').textContent(), 'Reunião revisada');
    assert.equal(await first.locator('textarea').inputValue(), edited);
    await page.locator('[data-rename-job]').click();
    assert.equal(await page.locator('[data-rename-dialog] [name=title]').inputValue(), 'Reunião revisada');
    await page.locator('[data-cancel-rename]').click();
    assert.equal(renameRequests, 2);
    await page.locator('[data-review-filter]').selectOption('pending');
    await page.locator('[data-speaker-filter]').selectOption(fixture.secondSpeaker);
    assert.equal(await first.isVisible(), false);
    await page.locator('[data-speaker-filter]').selectOption('all');
    assert.equal(await first.locator('textarea').inputValue(), edited);
    assert.ok(await first.isVisible());
    await first.locator('[data-reviewed]').check();
    assert.equal(await first.isVisible(), false);
    await page.locator('[data-review-filter]').selectOption('all');
    await first.locator('[data-reviewed]').uncheck();
    await first.locator('[data-listen]').click();
    await page.waitForFunction(() => { const a = document.querySelector('audio'); return !a.paused && a.currentTime >= .95 && a.currentTime < 3; });
    await page.waitForFunction(() => { const a = document.querySelector('audio'); return a.paused && a.currentTime >= 4.9; }, {timeout: 10000});
    const end = await page.locator('audio').evaluate(a => a.currentTime);
    assert.ok(end <= 5.3, 'playback must stop at the segment end plus one second');
    await page.locator('[data-playback-speed]').selectOption('1.25');
    await second.locator('[data-listen]').click();
    await page.waitForFunction(() => { const a = document.querySelector('audio'); return !a.paused && a.currentTime >= 5 && a.currentTime < 7 && a.playbackRate === 1.25; });
    assert.equal(await first.evaluate(e => e.classList.contains('is-playing')), false);
    await page.locator('audio').evaluate(a => a.pause());
    await page.locator('[data-repeat]').check();
    await first.locator('[data-listen]').click();
    await page.waitForTimeout(3700);
    const looping = await page.locator('audio').evaluate(a => ({time: a.currentTime, paused: a.paused}));
    assert.equal(looping.paused, false);
    assert.ok(looping.time < 4, 'repeat must return to the segment beginning');
    await page.locator('audio').evaluate(a => a.pause());
    const previousRequests = mediaRequests;
    await page.locator('audio').evaluate(a => a.dispatchEvent(new Event('error')));
    await page.getByText('Acesso ao áudio renovado. Clique para continuar a reprodução.').waitFor();
    assert.ok(mediaRequests > previousRequests, 'renewed playback must reload its private URL');
    // Exclusion is a pending form change, reversible without losing edits.
    await first.locator('[data-delete-segment]').click();
    assert.equal(await first.locator('[data-segment-body]').isVisible(), false);
    assert.equal(await first.locator('[data-deletion-notice]').isVisible(), true);
    assert.equal(await page.locator('audio').evaluate(a => a.paused), true);
    const deletionName = await first.locator('[data-delete-marker]').getAttribute('name');
    const pendingDeletion = await page.locator('form[data-review-form]').evaluate(form => Object.fromEntries(new FormData(form)));
    assert.equal(pendingDeletion[deletionName], '1');
    assert.equal(pendingDeletion[fixture.firstTextName], edited);
    assert.ok((await page.locator('[data-pending-summary]').textContent()).startsWith('14 '));
    await first.locator('[data-undo-delete]').click();
    assert.equal(await first.locator('textarea').inputValue(), edited);
    const undone = await page.locator('form[data-review-form]').evaluate(form => Object.fromEntries(new FormData(form)));
    assert.equal(deletionName in undone, false);
    assert.ok((await page.locator('[data-pending-summary]').textContent()).startsWith('15 '));
    const posted = await page.locator('form[data-review-form]').evaluate(form => Object.fromEntries(new FormData(form)));
    assert.equal(posted[fixture.firstTextName], edited);
    assert.ok(Object.keys(posted).length < 10, 'unchanged rows should not be posted');
    await page.locator('.speaker-settings > summary').click();
    await page.locator('[data-new-speaker-name]').fill('Pessoa revisada');
    await page.locator('[data-add-speaker]').click();
    await first.locator('select[name^="assign_"]').selectOption('new_1');
    const draft = await page.locator('form[data-review-form]').evaluate(form => Object.fromEntries(new FormData(form)));
    assert.equal(draft.new_speaker_new_1, 'Pessoa revisada');
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({path: '/tmp/transcription-review-mobile.png', fullPage: false});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'mobile must not scroll horizontally');
    assert.ok((await first.locator('textarea').boundingBox()).width > 280);
    await first.scrollIntoViewIfNeeded();
    await page.screenshot({path: '/tmp/transcription-review-mobile-editor.png', fullPage: false});
    // An old job can prepare playback without reloading or losing edits.
    await page.locator('audio').evaluate(a => a.removeAttribute('src'));
    mediaState = '';
    await page.locator('[data-prepare-audio]').click();
    await page.getByText('Preparando áudio para reprodução…', {exact: true}).waitFor();
    mediaState = 'ready';
    await page.getByText('Áudio pronto. Clique em “Ouvir trecho” para começar.').waitFor();
    assert.equal(prepareRequests, 1);
    assert.equal(await first.locator('textarea').inputValue(), edited);
    assert.deepEqual(errors, []);
    console.log('Review browser passed: desktop/mobile layout, collapsed notices, filters, edits, individual review, playback, stop, repeat, speed, renewal and preparation.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
