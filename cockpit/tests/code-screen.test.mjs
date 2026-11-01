import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

import { screenFiles } from '../builder/lib/screen.mjs';

const PROGRAM = new URL('../builder/code-screen.mjs', import.meta.url).pathname;

// Assembled at run time so this file holds no secret-shaped literal for the
// repository's own scanner, and every value is synthetic.
const SECRETS = {
  'GitHub personal token': `gh${'p'}_${'a1B2'.repeat(9)}`,
  'GitHub OAuth token': `gh${'o'}_${'a1B2'.repeat(9)}`,
  'GitHub server token': `gh${'s'}_${'a1B2'.repeat(9)}`,
  'GitHub fine-grained token': `github_${'pat'}_${'a1B2'.repeat(10)}`,
  'provider key': `s${'k'}-${'a1B2'.repeat(6)}`,
  'provider project key': `s${'k'}-proj-${'a1B2'.repeat(6)}`,
  'AWS access key': `AK${'IA'}${'A1B2'.repeat(4)}`,
  'PEM private key': `-----BEGIN ${'PRIVATE'} KEY-----`,
  'PEM RSA private key': `-----BEGIN RSA ${'PRIVATE'} KEY-----`,
  'PEM OpenSSH private key': `-----BEGIN OPENSSH ${'PRIVATE'} KEY-----`,
  'Slack bot token': `xo${'xb'}-${'1234567890'}-${'a1B2'.repeat(4)}`,
  'Slack user token': `xo${'xp'}-${'1234567890'}-${'a1B2'.repeat(4)}`,
};

const ORDINARY_CODE = [
  'def count(token: str, api_key: str | None = None) -> int:',
  '    password = token',
  '    secret: str = load_secret()',
  '    return len(password)',
  'const token: string = feed;',
  'apiKey = "abc"',
  'authorization: Bearer',
  'task-abcdefghijklmnopqrstuvwxyz0123',
  'risk-adjusted-score-for-a-long-name-here',
  'ghp_short',
  'AKIA_NOT_A_KEY',
  '-----BEGIN CERTIFICATE-----',
  '-----BEGIN PUBLIC KEY-----',
  'xoxo-thanks',
].join('\n');

function run(input) {
  const raw = typeof input === 'string' ? input : JSON.stringify(input);
  const done = spawnSync(process.execPath, [PROGRAM], { input: raw, encoding: 'utf8' });
  return { status: done.status, stdout: done.stdout, report: JSON.parse(done.stdout) };
}

test('the default credential rule of screenFiles is unchanged', () => {
  const [diagnostic] = screenFiles(new Map([['widget.tsx', 'const token: string = feed;']]));
  assert.equal(diagnostic.rule, 'screen.credential');
});

test('the secrets-only option lets ordinary code pass', () => {
  assert.deepEqual(screenFiles(new Map([['src/a.py', ORDINARY_CODE]]), { credentials: 'secrets' }), []);
});

for (const [label, secret] of Object.entries(SECRETS)) {
  test(`the secrets-only option finds a ${label}, without its value`, () => {
    const found = screenFiles(new Map([['src/a.py', `VALUE = "${secret}"\n`]]), { credentials: 'secrets' });
    assert.deepEqual(found.map((item) => [item.rule, item.file]), [['screen.secret', 'src/a.py']]);
    assert.ok(!JSON.stringify(found).includes(secret));
  });
}

test('the secrets-only option still applies every personal-data rule', () => {
  const found = screenFiles(
    new Map([
      ['a.py', 'x = "jordan@personal-mail.com"'],
      ['b.py', 'x = "+1 (415) 555-0134"'],
      ['c.py', 'x = "Synthetic Private Owner"'],
      ['d.py', 'x = "private-account-value"'],
    ]),
    { credentials: 'secrets', names: ['Synthetic Private Owner'], settingsValues: ['private-account-value'] },
  );
  assert.deepEqual(found.map((item) => item.rule), ['screen.email', 'screen.phone', 'screen.name', 'screen.setting']);
});

test('the program passes ordinary code', () => {
  const { status, report } = run({ files: { 'src/a.py': ORDINARY_CODE }, screen: { commitMessage: 'Fix it' } });
  assert.deepEqual(report, { ok: true, findings: [] });
  assert.equal(status, 0);
});

test('a finding names the file and the code, and never the value', () => {
  const secret = SECRETS['provider key'];
  const { status, stdout, report } = run({
    files: { 'src/a.py': `VALUE = "${secret}"\n` },
    screen: { names: ['Synthetic Private Owner'], commitMessage: 'Thanks to Synthetic Private Owner' },
  });
  assert.equal(status, 1);
  assert.equal(report.ok, false);
  assert.deepEqual(report.findings.map((item) => [item.file, item.code]), [
    ['src/a.py', 'screen.secret'],
    ['commit message', 'screen.name'],
  ]);
  assert.ok(!stdout.includes(secret));
  assert.ok(!stdout.includes('Synthetic Private Owner'));
});

test('a secret in the commit message is found', () => {
  const { report } = run({ files: {}, screen: { commitMessage: `Rotate ${SECRETS['AWS access key']}` } });
  assert.deepEqual(report.findings.map((item) => [item.file, item.code]), [['commit message', 'screen.secret']]);
});

test('a file path is screened, because the path is published too', () => {
  const { status, report } = run({
    files: { 'tests/jordan@personal-mail.com/test_a.py': 'VALUE = 1\n' },
  });
  assert.equal(status, 1);
  assert.deepEqual(report.findings.map((item) => item.code), ['screen.email']);
});

for (const [label, input] of Object.entries({
  'text that is not JSON': 'not json',
  'an array': [],
  'no files': { screen: {} },
  'files as a list': { files: [] },
  'a file that is not text': { files: { 'src/a.py': null } },
})) {
  test(`${label} is an unusable input, distinct from a finding`, () => {
    const { status, report } = run(input);
    assert.equal(status, 2);
    assert.deepEqual(report, { ok: false, findings: [] });
  });
}
