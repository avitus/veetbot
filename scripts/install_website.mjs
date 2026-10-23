// Reuse only a successful local install with matching inputs and a healthy tree.
// CI and WEBSITE_INSTALL_FORCE=1 always perform the same clean, locked install.
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// The npm workspaces this installer manages: the public site and Cockpit.
const workspace = process.argv[2] ?? 'website';
if (!['website', 'cockpit'].includes(workspace)) {
  console.error(`Unknown npm workspace: ${workspace}`);
  process.exit(2);
}
const website = fileURLToPath(new URL(`../${workspace}/`, import.meta.url));
const stamp = `${website}node_modules/.veetbot-install-key`;

function npm(args, stdio = 'pipe') {
  const result = spawnSync('npm', args, { cwd: website, encoding: 'utf8', stdio });
  if (result.error) throw result.error;
  return result;
}

function fingerprint() {
  const hash = createHash('sha256');
  for (const file of ['package.json', 'package-lock.json']) {
    hash.update(readFileSync(`${website}${file}`));
  }
  hash.update(JSON.stringify([1, process.version, process.platform, process.arch, process.execPath]));
  for (const args of [['--version'], ['config', 'list', '--json']]) {
    const result = npm(args);
    if (result.status !== 0) throw new Error(`npm ${args[0]} failed`);
    // Configuration may contain private values: persist only its digest.
    hash.update(result.stdout);
  }
  return hash.digest('hex');
}

try {
  const key = fingerprint();
  const forced = Boolean(process.env.CI) || process.env.WEBSITE_INSTALL_FORCE === '1';
  const reusable = !forced && existsSync(stamp) && readFileSync(stamp, 'utf8') === key;
  if (reusable && npm(['ls', '--all', '--json']).status === 0) {
    console.log(`${workspace} dependencies unchanged; reusing verified local install.`);
  } else {
    rmSync(stamp, { force: true });
    const result = npm(['ci', '--no-audit', '--no-fund'], 'inherit');
    if (result.status !== 0) process.exit(result.status ?? 1);
    writeFileSync(stamp, key);
  }
} catch (error) {
  console.error(`${workspace} dependency installation failed: ${error.message}`);
  process.exitCode = 1;
}
