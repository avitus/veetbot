// Serve only the built runtime and this bundle to an isolated WebKit page.
// No local file URLs, private screen inputs, live data or browser bridge.
import { existsSync, readFileSync } from 'node:fs';
import { fixtureProps } from './fixtures.mjs';

const runtime = new URL('../../dist/runtime/', import.meta.url);
const origin = 'https://cockpit.invalid';
const types = { html: 'text/html', js: 'text/javascript', css: 'text/css' };

export async function previewWidget(build, files, selection) {
  const { manifest, bundle, sha256 } = build;
  const fixture = selection.fixture ?? 'fixtures/typical.json';
  const size = selection.size ?? manifest.sizes[0];
  if (!fixture.startsWith('fixtures/') || !files.has(fixture) || !manifest.sizes.includes(size)) {
    return { diagnostics: [{ rule: 'preview.selection', message: 'Choose a packaged fixture and a declared size.' }] };
  }
  const props = fixtureProps(JSON.parse(files.get(fixture)), manifest);
  // The sandbox starts commands with an empty environment. The release image
  // installs the pinned browser with Playwright's hermetic location setting.
  if (existsSync(new URL('../../node_modules/playwright-core/.local-browsers/', import.meta.url))) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = '0';
  }
  const { webkit } = await import('playwright');
  const browser = await webkit.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 960, height: 540 }, serviceWorkers: 'block' });
    const index = JSON.parse(readFileSync(new URL('manifest.json', runtime), 'utf8'));
    const assets = new Map(Object.values(index.assets).map(({ name }) => [name, readFileSync(new URL(name, runtime))]));
    const bundleName = sha256 + '.js';
    assets.set(bundleName, Buffer.from(bundle));
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url());
      const name = url.pathname.slice(1);
      if (url.origin !== origin || !assets.has(name)) return route.abort();
      return route.fulfill({ body: assets.get(name), contentType: types[name.split('.').pop()] });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    await page.goto(origin + '/' + index.entry);
    await page.waitForFunction(() => window.cockpitNativeLog?.some((m) => m.type === 'ready'));
    await page.evaluate((snapshot) => window.cockpitHost.apply(snapshot), {
      type: 'snapshot', platform: 'mac', theme: 'light', offsetMs: 0, frozenAt: props.now, feeds: props.feeds,
      widgets: [{ slug: manifest.slug, title: manifest.title, revision: sha256, bundle: bundleName,
        size, feeds: manifest.feeds, settings: props.settings, changedAt: props.now }],
    });
    try {
      await page.waitForFunction(() => window.cockpitNativeLog?.some((m) => ['loaded', 'tile_error', 'quarantined'].includes(m.type)));
      const failed = () => page.evaluate(() =>
        window.cockpitNativeLog.some((m) => ['tile_error', 'quarantined'].includes(m.type)));
      const failure = { diagnostics: [{ rule: 'preview.render', file: fixture, size, message: 'WebKit reported a widget failure.' }] };
      if (await failed()) return failure;
      // The host acknowledges the script load before React commits its first
      // render. Wait for the real component to replace the loading placeholder.
      const slot = page.frameLocator('iframe').locator('.ck-slot[data-loaded="true"]');
      await slot.waitFor();
      const text = await slot.evaluate((el) => el.innerText.slice(0, 1000).toWellFormed());
      const png = await page.screenshot({ timeout: 8000 });
      if (await failed()) return failure;
      if (png.length > 524288) return { diagnostics: [{ rule: 'preview.too_large', file: fixture, size, message: 'The preview exceeds 512 KiB.' }] };
      return { preview: { fixture, size, text, png: png.toString('base64') } };
    } catch {
      return { diagnostics: [{ rule: 'preview.incomplete', file: fixture, size, message: 'WebKit could not finish rendering this fixture.' }] };
    }
  } finally {
    await browser.close();
  }
}
