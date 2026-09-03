import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const projectRoot = new URL("../", import.meta.url);

async function htmlFor(pathname) {
  const relativePath = pathname === "/" ? "index.html" : `${pathname.slice(1)}.html`;
  return readFile(new URL(`../out/${relativePath}`, import.meta.url), "utf8");
}

test("homepage identifies Veetbot and links its public policies", async () => {
  const [html, css] = await Promise.all([
    htmlFor("/"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(html, /<title>Veetbot \| Governed AI agent<\/title>/i);
  assert.match(html, /An agent that can act\. A system you can inspect\./i);
  assert.match(html, /Gmail/i);
  assert.match(html, /href="\/privacy"/i);
  assert.match(html, /href="\/tos"/i);
  assert.match(html, /href="https:\/\/docs\.veetbot\.com\//i);
  assert.doesNotMatch(css, /\.site-nav a:not\(:last-child\)\s*\{\s*display:\s*none/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("privacy page discloses Gmail access, processing, retention, and control", async () => {
  const html = await htmlFor("/privacy");

  assert.match(html, /<title>Privacy Policy \| Veetbot<\/title>/i);
  assert.match(html, /gmail\.readonly/i);
  assert.match(html, /gmail\.modify/i);
  assert.match(html, /gmail\.send/i);
  assert.match(html, /AI model provider/i);
  assert.match(html, /Google API Services User Data Policy/i);
  assert.match(html, /Limited Use requirements/i);
  assert.match(html, /revoke/i);
  assert.match(html, /delete/i);
  assert.match(html, /href="\/tos"/i);
});

test("terms page explains authorization, approvals, and service limits", async () => {
  const html = await htmlFor("/tos");

  assert.match(html, /<title>Terms of Service \| Veetbot<\/title>/i);
  assert.match(html, /authorize Veetbot/i);
  assert.match(html, /approval/i);
  assert.match(html, /Google/i);
  assert.match(html, /as is/i);
  assert.match(html, /href="\/privacy"/i);
});

test("finished site is a static DigitalOcean artifact with no Sites runtime", async () => {
  const [page, layout, packageJson, config] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../next.config.ts", import.meta.url), "utf8"),
  ]);

  assert.doesNotMatch(page, /codex-preview|SkeletonPreview/);
  assert.doesNotMatch(layout, /Starter Project|codex-preview/);
  assert.doesNotMatch(packageJson, /vinext|wrangler|sites-vite-plugin/);
  assert.match(config, /output:\s*["']export["']/);
  assert.match(config, /trailingSlash:\s*false/);
  await access(new URL("../out/index.html", import.meta.url));
  await access(new URL("../out/privacy.html", import.meta.url));
  await access(new URL("../out/tos.html", import.meta.url));
  await assert.rejects(access(new URL("app/_sites-preview", projectRoot)));
  await assert.rejects(access(new URL("../.openai/hosting.json", import.meta.url)));
  await assert.rejects(access(new URL("../worker/index.ts", import.meta.url)));
  await assert.rejects(access(new URL("../vite.config.ts", import.meta.url)));
});
