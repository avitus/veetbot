// The realm's network boundary, in a real browser engine. Every request the
// page makes is routed: the runtime origin serves content-addressed assets,
// and anything else is recorded as an escape and aborted, as the native
// content rule list would. Chromium always runs; WebKit, the engine
// WKWebView uses, runs wherever Playwright has it installed.
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { chromium, webkit } from 'playwright';

import { compileWidget } from '../builder/lib/compile.mjs';
import { buildWidget } from '../builder/lib/build.mjs';
import { readPackage } from '../builder/lib/package.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const runtime = path.join(root, 'dist/runtime');
const manifest = JSON.parse(readFileSync(path.join(runtime, 'manifest.json'), 'utf8'));
const ORIGIN = 'https://cockpit.invalid';
const TYPES = { html: 'text/html', js: 'text/javascript', css: 'text/css' };
const NOW = Date.parse('2026-10-22T18:00:00Z');
const NAVIGATION_ONLY = new Set(['navigate']);

const assets = new Map();
for (const { name } of Object.values(manifest.assets)) {
  assets.set(name, { body: readFileSync(path.join(runtime, name)), type: TYPES[name.split('.').pop()] });
}

async function adversarial(name) {
  const source = readFileSync(path.join(root, 'examples/adversarial', name, 'widget.tsx'), 'utf8');
  const { bundle, diagnostics } = await compileWidget(new Map([['widget.tsx', source]]), { typecheck: false });
  assert.deepEqual(diagnostics, [], name);
  return bundle;
}

async function placement(slug, bundle, extra = {}) {
  const { createHash } = await import('node:crypto');
  const sha = createHash('sha256').update(bundle).digest('hex');
  assets.set(`${sha}.js`, { body: Buffer.from(bundle), type: TYPES.js });
  return { slug, revision: sha.slice(0, 12), bundle: `${sha}.js`, title: slug, feeds: [], size: 'sm', settings: {}, changedAt: NOW, ...extra };
}

async function reference(slug, extra = {}) {
  const built = await buildWidget({ files: readPackage(path.join(root, 'examples/widgets', slug)).files, slug });
  assert.equal(built.ok, true, slug);
  return placement(slug, built.bundle, { feeds: built.manifest.feeds.map((feed) => ({ id: feed.id, history: feed.history ?? 0 })), ...extra });
}

const ENGINES = [['chromium', chromium]];
if (existsSync(webkit.executablePath())) ENGINES.push(['webkit', webkit]);

async function openBoard(engine, widgets, feeds = {}) {
  const browser = await engine.launch();
  const context = await browser.newContext();
  const escapes = [];
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === ORIGIN) {
      const asset = assets.get(url.pathname.slice(1));
      return asset ? route.fulfill({ status: 200, contentType: asset.type, body: asset.body }) : route.fulfill({ status: 404, body: '' });
    }
    escapes.push({ url: request.url(), type: request.resourceType() });
    return route.abort();
  });
  const page = await context.newPage();
  page.on('websocket', (socket) => escapes.push({ url: socket.url(), type: 'websocket' }));
  await page.goto(`${ORIGIN}/${manifest.entry}`);
  await page.waitForFunction(() => window.cockpitNativeLog?.some((message) => message.type === 'ready'));
  await page.evaluate((snapshot) => window.cockpitHost.apply(snapshot), {
    type: 'snapshot', theme: 'light', platform: 'mac', offsetMs: 0, widgets, feeds,
  });
  const log = () => page.evaluate(() => window.cockpitNativeLog ?? []);
  const settled = async (count) => {
    await page.waitForFunction(
      (expected) => (window.cockpitNativeLog ?? []).filter((message) => message.type === 'loaded' || message.type === 'tile_error').length >= expected,
      count,
      { timeout: 15000 },
    );
  };
  const realm = () => page.frames().find((frame) => frame !== page.mainFrame());
  return { browser, page, escapes, log, settled, realm };
}

for (const [name, engine] of ENGINES) {
  test(`${name}: no member of the adversarial corpus reaches the network`, { timeout: 60000 }, async () => {
    const corpus = readdirSync(path.join(root, 'examples/adversarial')).filter((member) => !NAVIGATION_ONLY.has(member));
    const widgets = [await reference('approvals-waiting')];
    for (const member of corpus) widgets.push(await placement(member, await adversarial(member)));
    const board = await openBoard(engine, widgets);
    try {
      await board.settled(widgets.length);
      await board.page.waitForTimeout(1500);
      assert.deepEqual(board.escapes, []);
      const removed = await board.realm().evaluate(() =>
        ['RTCPeerConnection', 'WebSocket', 'EventSource', 'Worker'].map((primitive) => typeof globalThis[primitive]),
      );
      assert.deepEqual(removed, ['undefined', 'undefined', 'undefined', 'undefined']);
      const tiles = await board.realm().$$eval('.ck-tile', (elements) => elements.length);
      assert.equal(tiles, widgets.length);
    } finally {
      await board.browser.close();
    }
  });

  test(`${name}: a declared feed with no snapshot yet renders as pending`, { timeout: 60000 }, async () => {
    const board = await openBoard(engine, [await reference('approvals-waiting')], {});
    try {
      await board.settled(1);
      const level = await board.realm().$eval('.ck-tile', (element) => element.getAttribute('data-level'));
      assert.equal(level, 'pending');
    } finally {
      await board.browser.close();
    }
  });

  test(`${name}: a widget sees only its declared feeds and its own settings`, { timeout: 60000 }, async () => {
    const snoop = await placement('snoop', await adversarial('snoop'), {
      feeds: [{ id: 'agent.weather', history: 1 }],
      settings: { place: 'Example City' },
    });
    const other = await reference('flight-tracker', { settings: { flight: 'EX 100' }, changedAt: NOW - 1 });
    const point = { observedAt: NOW - 600_000, data: { temperature: 60 } };
    const feeds = {
      'agent.weather': { observedAt: NOW, expectedIntervalSec: 900, data: { temperature: 64 }, history: [point, point, point] },
      'agent.flight-status': { observedAt: NOW, expectedIntervalSec: 300, data: { status: 'on_time', gate: 'B12', boardingAt: null } },
      'veetbot.approvals.pending': { observedAt: NOW, expectedIntervalSec: 30, data: { count: 3, items: [] } },
    };
    const board = await openBoard(engine, [snoop, other], feeds);
    try {
      await board.settled(2);
      const seen = await board.realm().$eval('.ck-tile-title', (element) => element.textContent);
      assert.deepEqual(JSON.parse(/** @type {string} */ (seen)), { feeds: ['agent.weather'], history: 1, settings: { place: 'Example City' } });
    } finally {
      await board.browser.close();
    }
  });

  test(`${name}: a navigating widget is quarantined and never reaches its destination`, { timeout: 60000 }, async () => {
    const steady = await reference('approvals-waiting', { changedAt: NOW - 60_000 });
    const navigate = await placement('navigate', await adversarial('navigate'), { changedAt: NOW });
    const board = await openBoard(engine, [steady, navigate]);
    try {
      await board.page.waitForFunction(() => (window.cockpitNativeLog ?? []).some((message) => message.type === 'quarantined'), null, { timeout: 15000 });
      const quarantined = (await board.log()).filter((message) => message.type === 'quarantined');
      assert.deepEqual(quarantined.map(({ slug, reason }) => ({ slug, reason })), [{ slug: 'navigate', reason: 'navigated' }]);
      // Whatever reached the network layer was only that navigation, and it was aborted.
      assert.ok(board.escapes.every((escape) => escape.type === 'document' && escape.url === 'https://evil.example/navigate'), JSON.stringify(board.escapes));
      // The remounted realm renders the remaining widget without the quarantined one.
      await board.page.waitForFunction(() => (window.cockpitNativeLog ?? []).filter((message) => message.type === 'loaded' && message.slug === 'approvals-waiting').length >= 2, null, { timeout: 15000 });
      assert.equal(await board.realm().$$eval('.ck-tile', (elements) => elements.length), 1);
    } finally {
      await board.browser.close();
    }
  });

  test(`${name}: a row opens its destination natively and the tile still selects`, { timeout: 60000 }, async () => {
    const approvals = await reference('approvals-waiting');
    const typical = JSON.parse(readFileSync(path.join(root, 'examples/widgets/approvals-waiting/fixtures/typical.json'), 'utf8'));
    const feed = typical.feeds['veetbot.approvals.pending'];
    const feeds = { 'veetbot.approvals.pending': { ...feed, observedAt: Date.parse(feed.observedAt) } };
    const board = await openBoard(engine, [{ ...approvals, size: 'md' }], feeds);
    try {
      await board.settled(1);
      await board.realm().click('.ck-row[data-opens]');
      await board.page.waitForFunction(() => (window.cockpitNativeLog ?? []).some((message) => message.type === 'open'), null, { timeout: 5000 });
      const opened = (await board.log()).filter((message) => message.type === 'open');
      assert.deepEqual(opened, [{ type: 'open', slug: 'approvals-waiting', kind: 'approval', id: feed.data.items[0].id }]);
      assert.ok(!(await board.log()).some((message) => message.type === 'select'), 'opening a row is not selecting the tile');
      await board.realm().click('.ck-tile-title');
      await board.page.waitForFunction(() => (window.cockpitNativeLog ?? []).some((message) => message.type === 'select'), null, { timeout: 5000 });
      // A forged intent from the realm names no kind native code opens, and is dropped.
      await board.realm().evaluate(() => window.parent.postMessage({ type: 'intent', key: 'approvals-waiting@x', kind: 'url', id: 'https://evil.example' }, '*'));
      await board.page.waitForTimeout(200);
      assert.equal((await board.log()).filter((message) => message.type === 'open').length, 1);
    } finally {
      await board.browser.close();
    }
  });
}
