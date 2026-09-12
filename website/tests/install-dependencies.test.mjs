import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repository = fileURLToPath(new URL('../../', import.meta.url));

function fixture(t) {
  const root = mkdtempSync(path.join(tmpdir(), 'veetbot-install-test-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(path.join(root, 'website'));
  mkdirSync(path.join(root, 'scripts'));
  mkdirSync(path.join(root, 'bin'));
  copyFileSync(path.join(repository, 'Makefile'), path.join(root, 'Makefile'));
  const installer = path.join(repository, 'scripts/install_website.mjs');
  if (existsSync(installer)) copyFileSync(installer, path.join(root, 'scripts/install_website.mjs'));
  for (const file of ['package.json', 'package-lock.json']) {
    writeFileSync(path.join(root, 'website', file), '{}');
  }
  const npm = `#!${process.execPath}
import { appendFileSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
const root = ${JSON.stringify(root)};
let args = process.argv.slice(2);
if (args[0] === '--prefix') args = args.slice(2);
if (args[0] === '--version') console.log(process.env.TEST_NPM_VERSION || '10.0.0');
else if (args[0] === 'config') console.log(JSON.stringify({ omit: process.env.TEST_OMIT || '' }));
else if (args[0] === 'ls') process.exit(existsSync(path.join(root, 'website/node_modules/present')) ? 0 : 1);
else if (args[0] === 'ci') {
  appendFileSync(path.join(root, 'installs'), 'ci\\n');
  if (process.env.TEST_INSTALL_FAIL) process.exit(19);
  mkdirSync(path.join(root, 'website/node_modules'), { recursive: true });
  writeFileSync(path.join(root, 'website/node_modules/present'), 'installed');
} else process.exit(23);
`;
  // .mjs makes the stub independent of the checkout's package type.
  writeFileSync(path.join(root, 'bin/npm.mjs'), npm, { mode: 0o755 });
  writeFileSync(path.join(root, 'bin/npm'), `#!/bin/sh\nexec '${process.execPath}' '${root}/bin/npm.mjs' "$@"\n`, { mode: 0o755 });
  return {
    root,
    count: () => readFileSync(path.join(root, 'installs'), 'utf8').trim().split('\n').length,
    run: (settings = {}) => {
      const env = { ...process.env, PATH: `${root}/bin:${process.env.PATH}`, CI: '', ...settings };
      delete env.MAKEFLAGS;
      delete env.MFLAGS;
      delete env.MAKELEVEL;
      return spawnSync('make', ['website-install'], { cwd: root, env, encoding: 'utf8', timeout: 15000 });
    },
  };
}

function succeeds(result) {
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
}

test('reuses a successful unchanged install', (t) => {
  const f = fixture(t);
  succeeds(f.run());
  succeeds(f.run());
  assert.equal(f.count(), 1);
});

for (const file of ['package.json', 'package-lock.json']) {
  test(`reinstalls when ${file} changes`, (t) => {
    const f = fixture(t);
    succeeds(f.run());
    writeFileSync(path.join(f.root, 'website', file), '{"changed":true}');
    succeeds(f.run());
    assert.equal(f.count(), 2);
  });
}

for (const settings of [{ TEST_NPM_VERSION: '11.0.0' }, { TEST_OMIT: 'dev' }, { CI: 'true' }, { WEBSITE_INSTALL_FORCE: '1' }]) {
  test(`reinstalls for ${JSON.stringify(settings)}`, (t) => {
    const f = fixture(t);
    succeeds(f.run());
    succeeds(f.run(settings));
    assert.equal(f.count(), 2);
  });
}

for (const missing of ['node_modules', 'node_modules/present']) {
  test(`repairs missing ${missing}`, (t) => {
    const f = fixture(t);
    succeeds(f.run());
    rmSync(path.join(f.root, 'website', missing), { recursive: true });
    succeeds(f.run());
    assert.equal(f.count(), 2);
  });
}

test('a failed forced reinstall invalidates the previous success', (t) => {
  const f = fixture(t);
  succeeds(f.run());
  assert.notEqual(f.run({ WEBSITE_INSTALL_FORCE: '1', TEST_INSTALL_FAIL: '1' }).status, 0);
  succeeds(f.run());
  assert.equal(f.count(), 3);
});
