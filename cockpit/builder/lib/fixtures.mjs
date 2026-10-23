// Fixtures are synthetic inputs a widget is rendered against: previews, the
// build's render check and the gallery. They may name only declared feeds.
import { readFileSync } from 'node:fs';

import Ajv from 'ajv';

const schema = JSON.parse(readFileSync(new URL('../../schema/fixture.schema.json', import.meta.url), 'utf8'));
const ajv = new Ajv({ allErrors: true, strict: true });
const validateShape = ajv.compile(schema);

function fixtureNames(files) {
  return [...files.keys()].filter((name) => name.startsWith('fixtures/')).sort();
}

/**
 * @param {Map<string, string>} files
 * @param {{feeds: {id: string, history?: number}[], settings: object}} manifest
 */
export function checkFixtures(files, manifest) {
  const declared = new Map(manifest.feeds.map((feed) => [feed.id, feed.history ?? 0]));
  const validateSettings = new Ajv({ allErrors: true, strict: false }).compile(manifest.settings);
  const diagnostics = [];
  for (const file of fixtureNames(files)) {
    const report = (rule, message) => diagnostics.push({ rule, file, message });
    let fixture;
    try {
      fixture = JSON.parse(/** @type {string} */ (files.get(file)));
    } catch (error) {
      report('fixture.invalid_json', /** @type {Error} */ (error).message);
      continue;
    }
    if (!validateShape(fixture)) {
      const first = validateShape.errors?.[0];
      report('fixture.shape', `${first?.instancePath || '/'} ${first?.message ?? 'is invalid'}`);
      continue;
    }
    for (const [id, feed] of Object.entries(fixture.feeds)) {
      if (!declared.has(id)) report('fixture.undeclared_feed', `feed ${id} is not declared in widget.json`);
      else if ((feed.history ?? []).length > /** @type {number} */ (declared.get(id))) {
        report('fixture.history', `feed ${id} supplies more history than widget.json declares`);
      }
    }
    if (!validateSettings(fixture.settings)) {
      const first = validateSettings.errors?.[0];
      report('fixture.settings', `settings${first?.instancePath ?? ''} ${first?.message ?? 'are invalid'}`);
    }
  }
  return diagnostics;
}

/**
 * The props a widget receives for a checked fixture, with instants as epoch milliseconds.
 * @param {any} fixture
 * @param {{feeds: {id: string}[]}} manifest
 */
export function fixtureProps(fixture, manifest) {
  const feeds = {};
  for (const { id } of manifest.feeds) {
    const feed = fixture.feeds[id];
    if (!feed) continue;
    feeds[id] = {
      observedAt: feed.observedAt === null ? null : Date.parse(feed.observedAt),
      expectedIntervalSec: feed.expectedIntervalSec,
      error: feed.error ?? null,
      data: feed.data ?? null,
      history: (feed.history ?? []).map((point) => ({ observedAt: Date.parse(point.observedAt), data: point.data })),
    };
  }
  return { now: Date.parse(fixture.now), settings: fixture.settings, feeds };
}
