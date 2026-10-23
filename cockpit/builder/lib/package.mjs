// The package-level rules: which files a widget may contain and how large.
import { lstatSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

export const REQUIRED_FILES = ['widget.json', 'widget.tsx', 'fixtures/typical.json', 'fixtures/empty.json', 'fixtures/stale.json'];
const ALLOWED = /^(widget\.json|widget\.tsx|fixtures\/[a-z0-9][a-z0-9-]{0,31}\.json)$/;
const MAX_FILES = 12;
const MAX_FILE_BYTES = 64 * 1024;
const MAX_PACKAGE_BYTES = 256 * 1024;

function diagnostic(rule, file, message) {
  return { rule, file, message };
}

function escapes(name) {
  return (
    name.startsWith('/') ||
    name.includes('\\') ||
    path.posix.normalize(name) !== name ||
    name.split('/').includes('..')
  );
}

/**
 * Check a package's file set.
 * @param {Map<string, string>} files package-relative POSIX paths to UTF-8 content
 */
export function checkFiles(files) {
  const diagnostics = [];
  let total = 0;
  for (const [name, content] of files) {
    if (escapes(name)) {
      diagnostics.push(diagnostic('package.path', name, 'paths must be relative and stay inside the package'));
      continue;
    }
    if (!ALLOWED.test(name)) {
      diagnostics.push(diagnostic('package.file_not_allowed', name, 'only widget.json, widget.tsx and fixtures/*.json are allowed'));
      continue;
    }
    const bytes = Buffer.byteLength(content, 'utf8');
    total += bytes;
    if (bytes > MAX_FILE_BYTES) {
      diagnostics.push(diagnostic('package.file_too_large', name, `${bytes} bytes exceeds ${MAX_FILE_BYTES}`));
    }
  }
  for (const name of REQUIRED_FILES) {
    if (!files.has(name)) diagnostics.push(diagnostic('package.missing_file', name, 'required file is missing'));
  }
  if (files.size > MAX_FILES) {
    diagnostics.push(diagnostic('package.too_many_files', '', `${files.size} files exceeds ${MAX_FILES}`));
  }
  if (total > MAX_PACKAGE_BYTES) {
    diagnostics.push(diagnostic('package.too_large', '', `${total} bytes exceeds ${MAX_PACKAGE_BYTES}`));
  }
  return diagnostics;
}

/**
 * Read a widget directory without following links.
 * @param {string} root
 */
export function readPackage(root) {
  const files = new Map();
  const diagnostics = [];
  const walk = (relative) => {
    for (const entry of readdirSync(path.join(root, relative))) {
      const name = relative ? `${relative}/${entry}` : entry;
      const stats = lstatSync(path.join(root, name));
      if (stats.isSymbolicLink()) diagnostics.push(diagnostic('package.symlink', name, 'symbolic links are not allowed'));
      else if (stats.isDirectory()) walk(name);
      else files.set(name, readFileSync(path.join(root, name), 'utf8'));
    }
  };
  walk('');
  return { files, diagnostics: [...diagnostics, ...checkFiles(files)] };
}
