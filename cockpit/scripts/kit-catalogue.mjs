// The kit catalogue the assistant reads through `cockpit.read` (describe_kit):
// each author-facing primitive with its props, defaults, description and example,
// generated from the kit's own source so it cannot drift from the types.
//   node scripts/kit-catalogue.mjs           write the catalogue
//   node scripts/kit-catalogue.mjs --check   exit 1 when the committed one is stale
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';

const KIT_SOURCE = fileURLToPath(new URL('../kit/src/index.tsx', import.meta.url));
export const CATALOGUE_PATH = fileURLToPath(new URL('../../src/agent_core/cockpit/kit-catalogue.json', import.meta.url));

// Only the realm and the host use these; a widget never renders them.
const INTERNAL = new Set(['CockpitRoot', 'CockpitClock', 'TileHost', 'FreshnessScope']);
const RULES = [
  'Default-export one function component that takes {feeds, settings, size, theme}.',
  "Import only from 'react' and '@veetbot/cockpit-kit'.",
  'Render through kit primitives and kit tokens; never write colour literals.',
  'No network, storage, timers that poll, eval, dynamic import, parent, top or opener.',
  'Read time with useNow(); fixtures freeze it, so rendering is deterministic.',
  "Name nothing token, secret, password, bearer or api_key followed by ':' or '=': the credential pattern rejects it.",
  'Packages are generic and public: personal values belong in private settings and feeds, never in code or fixtures.',
  'Declare every feed the widget reads, with the history depth it needs.',
];

function docs(node) {
  const jsDoc = /** @type {any} */ (node).jsDoc ?? [];
  const last = jsDoc.at(-1);
  if (!last) return { description: '', example: '' };
  const description = typeof last.comment === 'string' ? last.comment.trim() : ts.getTextOfJSDocComment(last.comment)?.trim() ?? '';
  const exampleTag = (last.tags ?? []).find((tag) => tag.tagName.text === 'example');
  const example = exampleTag ? (typeof exampleTag.comment === 'string' ? exampleTag.comment : ts.getTextOfJSDocComment(exampleTag.comment) ?? '') : '';
  return { description, example: example.trim() };
}

function propsFrom(members, defaults, source) {
  return members
    .filter((member) => ts.isPropertySignature(member) && member.name)
    .map((member) => {
      const name = member.name.getText(source);
      const prop = { name, type: member.type ? member.type.getText(source) : 'unknown', optional: Boolean(member.questionToken) };
      const description = docs(member).description;
      if (description) prop.description = description;
      if (defaults.has(name)) prop.default = defaults.get(name);
      return prop;
    })
    .filter((prop) => !['className', 'style'].includes(prop.name));
}

export function kitCatalogue() {
  const text = readFileSync(KIT_SOURCE, 'utf8');
  const source = ts.createSourceFile(KIT_SOURCE, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const interfaces = new Map();
  const types = {};
  let kitApi = 0;
  for (const statement of source.statements) {
    const exported = statement.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword);
    if (!exported) continue;
    if (ts.isInterfaceDeclaration(statement)) interfaces.set(statement.name.text, statement);
    if (ts.isTypeAliasDeclaration(statement)) types[statement.name.text] = statement.type.getText(source);
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (declaration.name.getText(source) === 'KIT_API' && declaration.initializer) kitApi = Number(declaration.initializer.getText(source));
      }
    }
  }
  const primitives = [];
  const helpers = [];
  for (const statement of source.statements) {
    if (!ts.isFunctionDeclaration(statement) || !statement.name) continue;
    if (!statement.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)) continue;
    const name = statement.name.text;
    const { description, example } = docs(statement);
    if (!/^[A-Z]/.test(name)) {
      const parameters = statement.parameters.map((parameter) => parameter.getText(source)).join(', ');
      const returns = statement.type ? `: ${statement.type.getText(source)}` : '';
      helpers.push({ name, signature: `${name}(${parameters})${returns}` });
      continue;
    }
    if (INTERNAL.has(name)) continue;
    const [parameter] = statement.parameters;
    const defaults = new Map();
    if (parameter && ts.isObjectBindingPattern(parameter.name)) {
      for (const element of parameter.name.elements) {
        if (element.initializer) defaults.set((element.propertyName ?? element.name).getText(source), element.initializer.getText(source));
      }
    }
    let props = [];
    const declared = parameter?.type;
    if (declared && ts.isTypeReferenceNode(declared) && interfaces.has(declared.typeName.getText(source))) {
      props = propsFrom(interfaces.get(declared.typeName.getText(source)).members, defaults, source);
    } else if (declared && ts.isTypeLiteralNode(declared)) {
      props = propsFrom(declared.members, defaults, source);
    }
    primitives.push({ name, description, props, example });
  }
  const shared = ['FeedFreshness', 'FeedView', 'WidgetProps', 'BarDatum', 'TimelineItem'];
  for (const name of shared) {
    const declaration = interfaces.get(name);
    if (declaration) types[name] = `{ ${declaration.members.map((member) => member.getText(source).replace(/\s+/g, ' ').replace(/;$/, '')).join('; ')} }`;
  }
  return {
    kit_api: kitApi,
    rules: RULES,
    primitives,
    helpers: helpers.filter((helper) => ['useNow', 'useFreshness', 'freshnessLevel', 'freshnessText', 'formatAge', 'formatInterval', 'formatClock', 'formatCountdown', 'ageSeconds'].includes(helper.name)),
    types: Object.fromEntries(Object.entries(types).sort(([a], [b]) => a.localeCompare(b))),
  };
}

function serialized() {
  return `${JSON.stringify(kitCatalogue(), null, 2)}\n`;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  if (process.argv.includes('--check')) {
    if (readFileSync(CATALOGUE_PATH, 'utf8') !== serialized()) {
      process.stderr.write('the kit catalogue is stale; run node cockpit/scripts/kit-catalogue.mjs\n');
      process.exit(1);
    }
  } else {
    writeFileSync(CATALOGUE_PATH, serialized());
  }
}
