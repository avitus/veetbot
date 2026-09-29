import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const projectRoot = new URL("../", import.meta.url);

async function htmlFor(pathname) {
  const relativePath = pathname === "/" ? "index.html" : `${pathname.slice(1)}.html`;
  return readFile(new URL(`../out/${relativePath}`, import.meta.url), "utf8");
}

const origin = "https://www.veetbot.com";
const publicPages = ["/", "/privacy", "/tos"];

function attributeValues(html, name) {
  return [...html.matchAll(new RegExp(`\\s${name}="([^"]*)"`, "g"))].map((match) => match[1]);
}

/** The exported file a same-origin path is served from, as `trailingSlash: false` writes it. */
function exportedFile(pathname) {
  if (pathname === "/") return "index.html";
  const file = decodeURIComponent(pathname.slice(1));
  return /\.[a-z0-9]+$/i.test(file) ? file : `${file}.html`;
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

/** Verify the built policy contains every reviewer-facing Gmail disclosure. */
test("privacy page discloses Gmail access, processing, retention, and control", async () => {
  const html = await htmlFor("/privacy");

  assert.match(html, /<title>Privacy Policy \| Veetbot<\/title>/i);
  assert.match(html, /gmail\.readonly/i);
  assert.match(html, /gmail\.modify/i);
  assert.match(html, /read, compose, and send all Gmail messages/i);
  assert.match(html, /gmail\.send/i);
  assert.match(html, /AI model provider/i);
  assert.match(html, /OpenAI and Anthropic/i);
  assert.match(html, /does not permit.*train.*general-purpose AI/i);
  assert.match(html, /provider.*up to 30 days/i);
  assert.match(html, /does not create cross-user aggregate or anonymized Gmail datasets/i);
  assert.match(html, /data brokers/i);
  assert.match(html, /cold email/i);
  assert.match(html, /unsubscribe request.*only when you ask/i);
  assert.match(html, /sender can see the address of your Veetbot server/i);
  assert.match(html, /never follows an unsubscribe link in a message body/i);
  assert.match(html, /Gmail content.*untrusted/i);
  assert.match(html, /cannot authorize an action/i);
  assert.match(html, /encrypted at rest/i);
  assert.match(html, /until you delete the session/i);
  assert.match(html, /no more than 35 days/i);
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

test("email mode discloses historical learning and distinct retention windows", async () => {
  const html = await htmlFor("/privacy");
  assert.match(html, /Email mode.*while.*active/i);
  assert.match(html, /received, archived, and Sent/i);
  assert.match(html, /writing style.*memories.*Chat/i);
  assert.match(html, /hosted.*model/i);
  assert.match(html, /30 days after.*last access/i);
  assert.match(html, /sent or discarded.*30 days/i);
  assert.match(html, /operational.*until.*session.*source exclusion/i);
  assert.match(html, /no more than 35 days/i);
  assert.match(html, /pause.*learning.*reset/i);
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

/** A broken internal link or asset would ship as a 404 on the public site. */
test("every same-origin link and asset on each public page resolves in the export", async () => {
  for (const pathname of publicPages) {
    const html = await htmlFor(pathname);
    const targets = [...attributeValues(html, "href"), ...attributeValues(html, "src")];
    const local = targets.filter((target) => target.startsWith("/") && !target.startsWith("//"));
    assert.ok(local.length > 0, `${pathname} has no same-origin links or assets`);
    for (const target of local) {
      const file = exportedFile(new URL(target, origin).pathname);
      await access(new URL(`../out/${file}`, import.meta.url)).catch(() =>
        assert.fail(`${pathname} refers to ${target}, which the export does not contain`),
      );
    }
    for (const fragment of targets.filter((target) => target.startsWith("#"))) {
      assert.match(html, new RegExp(` id="${fragment.slice(1)}"`), `${pathname} links to a missing ${fragment}`);
    }
  }
});

test("each public page describes itself and names its own canonical address", async () => {
  const descriptions = new Set();
  for (const pathname of publicPages) {
    const html = await htmlFor(pathname);
    assert.match(html, /<html lang="en"/);
    assert.match(html, /<meta name="viewport" content="width=device-width, initial-scale=1"\/>/);
    const description = html.match(/<meta name="description" content="([^"]+)"/)?.[1];
    assert.ok(description && description.length >= 40, `${pathname} has no useful description`);
    descriptions.add(description);
    const canonical = new URL(html.match(/<link rel="canonical" href="([^"]+)"/)?.[1] ?? "about:blank");
    assert.equal(canonical.origin, origin, `${pathname} has no canonical address on the public origin`);
    assert.equal(canonical.pathname, pathname);
    assert.match(html, /<meta property="og:image" content="https:\/\/www\.veetbot\.com\/og\.png"\/>/);
    assert.match(html, /<meta name="twitter:card" content="summary_large_image"\/>/);
    assert.match(html, /<link rel="icon" href="\/veetbot-icon\.svg"\/>/);
  }
  assert.equal(descriptions.size, publicPages.length, "each page needs its own description");
  const preview = await readFile(new URL("../out/og.png", import.meta.url));
  assert.equal(preview.subarray(1, 4).toString("ascii"), "PNG", "the shared preview must be a PNG");
});

/** The privacy policy says the site has no forms, analytics, or advertising. */
test("public pages load only their own code and collect nothing, as the privacy policy states", async () => {
  for (const pathname of publicPages) {
    const html = await htmlFor(pathname);
    const loaded = [
      ...attributeValues(html, "src"),
      ...[...html.matchAll(/<link rel="(?:stylesheet|preload|icon|shortcut icon|modulepreload)"[^>]*\shref="([^"]*)"/g)]
        .map((match) => match[1]),
    ];
    assert.ok(loaded.length > 0);
    for (const resource of loaded) {
      assert.ok(resource.startsWith("/") && !resource.startsWith("//"), `${pathname} loads ${resource} from elsewhere`);
    }
    assert.doesNotMatch(html, /<form\b|<input\b|<iframe\b|<textarea\b/i);
    assert.doesNotMatch(html, /googletagmanager|google-analytics|gtag\(|plausible\.io|segment\.(?:io|com)|document\.cookie/i);
  }
});

test("each legal page is dated and reachable from every page's footer", async () => {
  for (const [pathname, other] of [["/privacy", "/tos"], ["/tos", "/privacy"]]) {
    const html = await htmlFor(pathname);
    const effective = html.match(/<p class="effective-date">Effective ((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4})<\/p>/)?.[1];
    assert.ok(effective && !Number.isNaN(Date.parse(effective)), `${pathname} states no effective date`);
    assert.match(html, /<nav[^>]*aria-label="Primary navigation"[\s\S]*?href="\/privacy"[\s\S]*?href="\/tos"[\s\S]*?<\/nav>/);
    assert.ok(attributeValues(html, "href").filter((href) => href === "/").length >= 2, `${pathname} must link home`);
    assert.ok(html.includes(`href="${other}"`));
  }
  for (const pathname of publicPages) {
    const footer = (await htmlFor(pathname)).match(/<footer[\s\S]*?<\/footer>/)?.[0] ?? "";
    assert.match(footer, /href="\/privacy"/, `${pathname} footer omits the privacy policy`);
    assert.match(footer, /href="\/tos"/, `${pathname} footer omits the terms`);
  }
});
