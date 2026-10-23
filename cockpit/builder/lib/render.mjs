// Render every fixture at every declared size with the server renderer. A
// throw or an empty render rejects the revision before anything is committed.
import vm from 'node:vm';

import React, { createElement as h } from 'react';
import * as ReactJSXRuntime from 'react/jsx-runtime';
import { renderToString } from 'react-dom/server';

import { fixtureProps } from './fixtures.mjs';

const MAX_RENDER_BYTES = 512 * 1024;

/**
 * @param {Map<string, string>} files
 * @param {string} bundle the compiled widget
 * @param {{sizes: string[], feeds: {id: string}[]}} manifest
 */
export async function renderFixtures(files, bundle, manifest) {
  const kit = await import(new URL('../../dist/kit/index.mjs', import.meta.url).href);
  const registered = [];
  const context = vm.createContext({
    React,
    ReactJSXRuntime,
    VeetbotKit: kit,
    VeetbotRealm: { register: (component) => registered.push(component) },
  });
  try {
    vm.runInContext(bundle, context, { timeout: 2000 });
  } catch (error) {
    return [{ rule: 'render.load', file: 'widget.tsx', message: String(/** @type {Error} */ (error).message ?? error) }];
  }
  const [Widget] = registered;
  if (typeof Widget !== 'function') {
    return [{ rule: 'render.no_component', file: 'widget.tsx', message: 'the bundle registered no component' }];
  }
  const frame = (props, size, child) =>
    h(kit.CockpitRoot, { theme: 'light', platform: 'mac' },
      h(kit.CockpitClock, { frozenAt: props.now }, h(kit.TileHost, { size }, child)));
  const diagnostics = [];
  for (const file of [...files.keys()].filter((name) => name.startsWith('fixtures/')).sort()) {
    const props = fixtureProps(JSON.parse(/** @type {string} */ (files.get(file))), manifest);
    for (const size of manifest.sizes) {
      const report = (rule, message) => diagnostics.push({ rule, file, size, message });
      let html;
      try {
        html = renderToString(frame(props, size, h(Widget, { feeds: props.feeds, settings: props.settings, size, theme: 'light' })));
      } catch (error) {
        report('render.throws', `${size}: ${/** @type {Error} */ (error).message}`);
        continue;
      }
      if (html === renderToString(frame(props, size, null))) report('render.empty', `${size}: the widget rendered nothing`);
      else if (Buffer.byteLength(html) > MAX_RENDER_BYTES) report('render.too_large', `${size}: render exceeds ${MAX_RENDER_BYTES} bytes`);
    }
  }
  return diagnostics;
}
