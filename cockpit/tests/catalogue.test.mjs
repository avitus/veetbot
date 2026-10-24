import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { compileWidget } from '../builder/lib/compile.mjs';
import { CATALOGUE_PATH, kitCatalogue } from '../scripts/kit-catalogue.mjs';

const catalogue = kitCatalogue();

test('the committed kit catalogue is the one the kit generates', () => {
  const committed = JSON.parse(readFileSync(CATALOGUE_PATH, 'utf8'));
  assert.deepEqual(committed, catalogue, 'run: node cockpit/scripts/kit-catalogue.mjs');
});

test('every author-facing primitive is described with its props and an example', () => {
  const names = catalogue.primitives.map((primitive) => primitive.name);
  for (const expected of ['Tile', 'Metric', 'Sparkline', 'List', 'Row', 'StatusPill', 'Split']) {
    assert.ok(names.includes(expected), expected);
  }
  for (const internal of ['CockpitRoot', 'CockpitClock', 'TileHost', 'FreshnessScope']) {
    assert.ok(!names.includes(internal), internal);
  }
  for (const primitive of catalogue.primitives) {
    assert.ok(primitive.description, primitive.name);
    assert.ok(primitive.example.startsWith(`<${primitive.name}`), primitive.name);
  }
  const tile = catalogue.primitives.find((primitive) => primitive.name === 'Tile');
  const title = tile.props.find((prop) => prop.name === 'title');
  assert.deepEqual(title, { name: 'title', type: 'string', optional: false });
  const metric = catalogue.primitives.find((primitive) => primitive.name === 'Metric');
  assert.equal(metric.props.find((prop) => prop.name === 'size').default, "'l'");
  assert.equal(catalogue.kit_api, 1);
  assert.ok(catalogue.rules.some((rule) => rule.includes('token')));
});

test('every example type-checks against the kit', async () => {
  const names = [...new Set(catalogue.primitives.map((primitive) => primitive.name))];
  const body = catalogue.primitives.map((primitive) => `      ${primitive.example}`).join('\n');
  const source = `import { ${names.join(', ')} } from '@veetbot/cockpit-kit';\n\nexport default function Gallery() {\n  return (\n    <>\n${body}\n    </>\n  );\n}\n`;
  const compiled = await compileWidget(new Map([['widget.tsx', source]]));
  assert.deepEqual(compiled.diagnostics ?? [], []);
});
