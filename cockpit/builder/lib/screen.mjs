// The privacy screen. Widget code is published on a public branch, so a
// commit must never carry personal data. Diagnostics name the rule and the
// file, never the matched text.
import { readFileSync } from 'node:fs';

const credential = JSON.parse(readFileSync(new URL('../../schema/credential-pattern.json', import.meta.url), 'utf8'));
const CREDENTIAL = new RegExp(credential.pattern, credential.flags);
const EMAIL = /[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})/g;
// RFC 2606 names are reserved for documentation, so synthetic fixtures may use them.
const RESERVED_DOMAIN = /(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.(example|test|invalid|localhost))$|^(example|test|invalid|localhost)$/i;
const INTERNATIONAL_PHONE = /\+\d[\d\s().-]{6,18}\d/g;
const NANP_PHONE = /(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b/;
const MIN_TERM = 4;
// Shapes no ordinary code has, for text where `token: str` is an honest identifier
// (ADR-0281). Each anchors on a vendor's own prefix and a length no name reaches.
const SECRET_PATTERNS = [
  /(?<![A-Za-z0-9_-])gh[pos]_[A-Za-z0-9]{30,}/,
  /(?<![A-Za-z0-9_-])github_pat_[A-Za-z0-9_]{30,}/,
  /(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}/,
  /(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])/,
  /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----/,
  /(?<![A-Za-z0-9_-])xox[abeoprs]-[A-Za-z0-9-]{10,}/,
];

function escape(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function termPattern(term) {
  return new RegExp(`(?<![\\p{L}\\p{N}])${escape(term)}(?![\\p{L}\\p{N}])`, 'iu');
}

function screenable(term) {
  return typeof term === 'string' && (term.trim().length >= MIN_TERM || /\s/.test(term.trim()));
}

function hasPersonalEmail(text) {
  for (const match of text.matchAll(EMAIL)) {
    if (!RESERVED_DOMAIN.test(match[1])) return true;
  }
  return false;
}

function hasPhone(text) {
  for (const match of text.matchAll(INTERNATIONAL_PHONE)) {
    const digits = match[0].replace(/\D/g, '').length;
    if (digits >= 8 && digits <= 15) return true;
  }
  return NANP_PHONE.test(text);
}

/**
 * Screen every file of a package, and the commit message the outbox will write.
 * @param {Map<string, string>} files
 * @param {{names?: string[], settingsValues?: unknown[], commitMessage?: string, credentials?: 'identifiers' | 'secrets'}} context the
 *   principal's known People and contact names, its current private settings values, and
 *   the commit message, reported as the file `commit message`. `credentials` keeps the
 *   default rule, which refuses credential-shaped identifiers, or replaces it with one
 *   that finds only high-confidence secrets.
 */
export function screenFiles(files, { names = [], settingsValues = [], commitMessage, credentials = 'identifiers' } = {}) {
  const namePatterns = names.filter(screenable).map((name) => termPattern(name.trim()));
  const settingPatterns = settingsValues.filter(screenable).map((value) => termPattern(/** @type {string} */ (value).trim()));
  const diagnostics = [];
  const screened = commitMessage === undefined ? files : new Map([...files, ['commit message', commitMessage]]);
  for (const [file, text] of screened) {
    const found = (rule, message) => diagnostics.push({ rule, file, message: `${file} ${message}` });
    if (credentials === 'secrets') {
      if (SECRET_PATTERNS.some((pattern) => pattern.test(text))) found('screen.secret', 'contains what looks like a real secret; remove it and rotate it');
    } else if (CREDENTIAL.test(text)) {
      found('screen.credential', 'contains credential-shaped text; rename the identifier');
    }
    if (hasPersonalEmail(text)) found('screen.email', 'contains an email address outside the reserved example domains');
    if (hasPhone(text)) found('screen.phone', 'contains a telephone number');
    if (namePatterns.some((pattern) => pattern.test(text))) found('screen.name', 'contains the name of a known person');
    if (settingPatterns.some((pattern) => pattern.test(text))) found('screen.setting', 'contains a private settings value');
  }
  return diagnostics;
}
