// Manifest validation: the JSON Schema shared with the server, plus the
// cross-field rules a schema cannot state.
import { readFileSync } from 'node:fs';

import Ajv from 'ajv';

const schema = JSON.parse(readFileSync(new URL('../../schema/widget.schema.json', import.meta.url), 'utf8'));
const validate = new Ajv({ allErrors: true, strict: true }).compile(schema);

function diagnostic(rule, message) {
  return { rule, file: 'widget.json', message };
}

// Ajv reports every failed branch of a oneOf; report one diagnostic per field.
function fieldOf(error) {
  const [, first] = error.instancePath.split('/');
  if (first) return `/${first}`;
  if (error.keyword === 'additionalProperties') return `/${error.params.additionalProperty}`;
  if (error.keyword === 'required') return `/${error.params.missingProperty}`;
  return '/';
}

/**
 * @param {unknown} manifest the parsed `widget.json`
 * @param {string} slug the widget's directory name
 * @returns {{rule: string, file: string, message: string}[]}
 */
export function validateManifest(manifest, slug) {
  if (!validate(manifest)) {
    const fields = new Map();
    for (const error of validate.errors ?? []) {
      const field = fieldOf(error);
      if (!fields.has(field)) fields.set(field, `${field} ${error.message}`);
    }
    return [...fields.values()].map((message) => diagnostic('manifest.schema', message));
  }
  const diagnostics = [];
  const record = /** @type {{slug: string, feeds: {id: string}[]}} */ (manifest);
  if (record.slug !== slug) {
    diagnostics.push(diagnostic('manifest.slug_mismatch', `slug ${record.slug} does not match its directory`));
  }
  const seen = new Set();
  for (const feed of record.feeds) {
    if (seen.has(feed.id)) diagnostics.push(diagnostic('manifest.duplicate_feed', `feed ${feed.id} is declared twice`));
    seen.add(feed.id);
  }
  return diagnostics;
}
