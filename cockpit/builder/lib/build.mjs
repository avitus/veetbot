// The builder pipeline, in the order cockpit.md specifies: package and
// manifest, source lint, the credential pattern and privacy screen, fixtures,
// then the typecheck and bundle, then the render check. Any diagnostic rejects
// the revision; nothing past a failing stage runs.
import { createHash } from 'node:crypto';

import { compileWidget } from './compile.mjs';
import { checkFixtures } from './fixtures.mjs';
import { lintSource } from './lint.mjs';
import { validateManifest } from './manifest.mjs';
import { checkFiles } from './package.mjs';
import { renderFixtures } from './render.mjs';
import { screenFiles } from './screen.mjs';

export const KIT_API = 1;

function rejected(diagnostics, manifest) {
  return { ok: false, diagnostics, manifest };
}

/**
 * @param {{files: Map<string, string>, slug: string, screen?: {names?: string[], settingsValues?: unknown[]}}} input
 */
export async function buildWidget({ files, slug, screen = {} }) {
  const diagnostics = [...checkFiles(files)];
  let manifest;
  if (files.has('widget.json')) {
    try {
      manifest = JSON.parse(/** @type {string} */ (files.get('widget.json')));
    } catch (error) {
      diagnostics.push({ rule: 'manifest.invalid_json', file: 'widget.json', message: /** @type {Error} */ (error).message });
    }
    if (manifest !== undefined) diagnostics.push(...validateManifest(manifest, slug));
  }
  if (files.has('widget.tsx')) diagnostics.push(...lintSource(/** @type {string} */ (files.get('widget.tsx'))));
  diagnostics.push(...screenFiles(files, screen));
  if (diagnostics.length) return rejected(diagnostics, manifest);

  diagnostics.push(...checkFixtures(files, manifest));
  if (diagnostics.length) return rejected(diagnostics, manifest);

  const compiled = await compileWidget(files);
  if (compiled.diagnostics.length || !compiled.bundle) return rejected(compiled.diagnostics, manifest);

  const rendered = await renderFixtures(files, compiled.bundle, manifest);
  if (rendered.length) return rejected(rendered, manifest);

  const sha256 = createHash('sha256').update(compiled.bundle).digest('hex');
  return { ok: true, diagnostics: [], manifest, bundle: compiled.bundle, sha256, kit_api: KIT_API };
}
