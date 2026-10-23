// Build the Cockpit runtime from `kit/` and `host/`:
//   dist/kit/index.mjs      the kit as ESM for Node (tests, fixture rendering)
//   dist/kit-types/         declarations widgets are type-checked against
//   dist/runtime/           content-addressed browser assets and their manifest
import { createHash } from 'node:crypto';
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';
import ts from 'typescript';

const root = fileURLToPath(new URL('..', import.meta.url));
const dist = path.join(root, 'dist');
const runtime = path.join(dist, 'runtime');

// Every load the realm may make is one of its own content-addressed assets.
// Connections, frames, workers, forms and fonts are denied outright; there is
// no 'unsafe-eval' and no 'unsafe-inline'. React's style props go through the
// CSSOM, which style-src does not govern.
const SELF = "'self' veetbot-cockpit:";
const REALM_CSP = [
  "default-src 'none'",
  `script-src ${SELF}`,
  `style-src ${SELF}`,
  `img-src ${SELF} data:`,
  "connect-src 'none'",
  "font-src 'none'",
  "media-src 'none'",
  "frame-src 'none'",
  "child-src 'none'",
  "worker-src 'none'",
  "object-src 'none'",
  "manifest-src 'none'",
  "form-action 'none'",
  "base-uri 'none'",
].join('; ');
const HOST_CSP = REALM_CSP.replace("frame-src 'none'", `frame-src ${SELF}`).replace("child-src 'none'", `child-src ${SELF}`);

function document({ csp, head, body }) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="viewport" content="width=device-width, initial-scale=1">${head}</head><body>${body}</body></html>\n`;
}

function sha256(content) {
  return createHash('sha256').update(content).digest('hex');
}

async function bundle(entryPoint, options) {
  const result = await build({
    entryPoints: [path.join(root, entryPoint)],
    bundle: true,
    write: false,
    minify: true,
    legalComments: 'none',
    target: ['es2022', 'safari17'],
    jsx: 'automatic',
    define: { 'process.env.NODE_ENV': '"production"' },
    logLevel: 'silent',
    ...options,
  });
  return result.outputFiles[0].contents;
}

function emitKitTypes() {
  const program = ts.createProgram([path.join(root, 'kit/src/index.tsx')], {
    declaration: true,
    emitDeclarationOnly: true,
    jsx: ts.JsxEmit.ReactJSX,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    target: ts.ScriptTarget.ES2022,
    strict: true,
    skipLibCheck: true,
    outDir: path.join(dist, 'kit-types'),
  });
  const result = program.emit();
  const errors = ts.getPreEmitDiagnostics(program).concat(result.diagnostics);
  if (errors.length) {
    throw new Error(ts.formatDiagnostics(errors, {
      getCanonicalFileName: (name) => name,
      getCurrentDirectory: () => root,
      getNewLine: () => '\n',
    }));
  }
}

/** Write one content-addressed asset and return its published name. */
function publish(assets, logicalName, extension, content) {
  const bytes = typeof content === 'string' ? Buffer.from(content, 'utf8') : Buffer.from(content);
  const digest = sha256(bytes);
  const name = `${digest}.${extension}`;
  writeFileSync(path.join(runtime, name), bytes);
  assets[logicalName] = { name, sha256: digest, bytes: bytes.length };
  return name;
}

export async function buildRuntime() {
  rmSync(dist, { recursive: true, force: true });
  mkdirSync(runtime, { recursive: true });
  mkdirSync(path.join(dist, 'kit'), { recursive: true });

  writeFileSync(
    path.join(dist, 'kit/index.mjs'),
    await bundle('kit/src/index.tsx', { format: 'esm', platform: 'node', minify: false, external: ['react', 'react-dom'] }),
  );
  emitKitTypes();

  const assets = {};
  const kitCss = publish(assets, 'kit.css', 'css', readFileSync(path.join(root, 'kit/src/cockpit-kit.css')));
  const runtimeCss = publish(assets, 'runtime.css', 'css', readFileSync(path.join(root, 'host/src/runtime.css')));
  const browser = { format: 'iife', platform: 'browser' };
  const realmJs = publish(assets, 'realm.js', 'js', await bundle('host/src/realm.tsx', browser));
  const hostJs = publish(assets, 'host.js', 'js', await bundle('host/src/host.ts', browser));
  const realm = publish(assets, 'realm.html', 'html', document({
    csp: REALM_CSP,
    head: `<link rel="stylesheet" href="${kitCss}"><link rel="stylesheet" href="${runtimeCss}">`,
    body: `<div id="realm"></div><script src="${realmJs}"></script>`,
  }));
  const host = publish(assets, 'host.html', 'html', document({
    csp: HOST_CSP,
    head: `<meta name="cockpit-realm" content="${realm}"><link rel="stylesheet" href="${runtimeCss}">`,
    body: `<script src="${hostJs}"></script>`,
  }));
  return { assets, entry: host };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { assets, entry } = await buildRuntime();
  writeFileSync(path.join(runtime, 'manifest.json'), `${JSON.stringify({ kit_api: 1, entry, assets }, null, 2)}\n`);
  cpSync(path.join(root, 'schema'), path.join(dist, 'schema'), { recursive: true });
  console.log(`Cockpit runtime built: ${Object.keys(assets).length} assets.`);
}
