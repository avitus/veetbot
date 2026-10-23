import assert from 'node:assert/strict';
import test from 'node:test';

import { screenFiles } from '../builder/lib/screen.mjs';

const CONTEXT = { names: ['Jordan Avery', 'Priya', 'Al'], settingsValues: ['UA 1234', 'SFO', 4] };

function screen(content, file = 'fixtures/typical.json') {
  return screenFiles(new Map([[file, content]]), CONTEXT);
}

function rules(content, file) {
  return screen(content, file).map((diagnostic) => diagnostic.rule);
}

test('synthetic, generic content passes', () => {
  const clean = JSON.stringify({
    feeds: { 'agent.flight': { data: { carrier: 'Example Air', gate: 'B12', departs: '2026-10-22T18:05:00Z' } } },
    settings: { flight: 'EX 100' },
    contact: 'alex@example.com',
    other: 'sam@team.example',
    ticks: 86400000,
  });
  assert.deepEqual(screen(clean), []);
  assert.deepEqual(screen('const tokenCount = 3;\nconst label = `${tokenCount} tokens`;', 'widget.tsx'), []);
});

test('credential-shaped text is refused before the hardline rule sees it', () => {
  for (const text of ['const token: string = feed;', 'apiKey = "abc"', 'const inputToken: number = 3;', 'Bearer: x']) {
    assert.deepEqual(rules(text, 'widget.tsx'), ['screen.credential'], text);
  }
});

test('real email addresses and phone numbers are refused', () => {
  assert.deepEqual(rules('"from": "jordan@personal-mail.com"'), ['screen.email']);
  for (const phone of ['+1 (415) 555-0134', '+447700900123', '415.555.0134']) {
    assert.deepEqual(rules(`"phone": "${phone}"`), ['screen.phone'], phone);
  }
});

test('known people are refused on word boundaries, ignoring case', () => {
  assert.deepEqual(rules('"who": "jordan avery"'), ['screen.name']);
  assert.deepEqual(rules('"who": "PRIYA"'), ['screen.name']);
  assert.deepEqual(rules('"who": "Priyanka"'), []);
  assert.deepEqual(rules('"note": "Also"'), [], 'names shorter than four characters are not screened alone');
});

test('current private settings values are refused', () => {
  assert.deepEqual(rules('const flight = "UA 1234";', 'widget.tsx'), ['screen.setting']);
  assert.deepEqual(rules('"code": "SFO"'), [], 'short values are not screened');
});

test('a diagnostic never repeats what it matched', () => {
  const text = 'jordan@personal-mail.com Jordan Avery UA 1234 +447700900123 token: abc';
  const diagnostics = screen(text);
  assert.deepEqual(diagnostics.map((diagnostic) => diagnostic.rule).sort(), [
    'screen.credential', 'screen.email', 'screen.name', 'screen.phone', 'screen.setting',
  ]);
  for (const diagnostic of diagnostics) {
    for (const secret of ['jordan@', 'Jordan', 'UA 1234', '7700900123', 'abc']) {
      assert.ok(!diagnostic.message.includes(secret), `${diagnostic.rule} leaked ${secret}`);
    }
  }
});
