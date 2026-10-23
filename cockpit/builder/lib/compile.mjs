// Type-check a widget against the kit's declarations, then bundle it into a
// classic script that registers itself with the realm. React and the kit are
// the realm's globals, never bundled.
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';
import ts from 'typescript';

const ROOT = fileURLToPath(new URL('../..', import.meta.url));
const VIRTUAL = path.join(ROOT, '.widget-check');
const WIDGET = path.join(VIRTUAL, 'widget.tsx');
const CHECK = path.join(VIRTUAL, 'check.ts');
const CHECK_SOURCE = `import Widget from './widget';
import type { CockpitWidget } from '@veetbot/cockpit-kit';
export const widget: CockpitWidget = Widget;
`;
const MAX_BUNDLE_BYTES = 256 * 1024;
const GLOBALS = new Map([
  ['react', 'React'],
  ['react/jsx-runtime', 'ReactJSXRuntime'],
  ['@veetbot/cockpit-kit', 'VeetbotKit'],
]);

const OPTIONS = {
  strict: true,
  noEmit: true,
  jsx: ts.JsxEmit.ReactJSX,
  target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler,
  skipLibCheck: true,
  lib: ['lib.es2022.d.ts', 'lib.dom.d.ts'],
  types: [],
  baseUrl: ROOT,
  paths: { '@veetbot/cockpit-kit': ['./dist/kit-types/index.d.ts'] },
};

function typecheck(source) {
  const virtual = new Map([[WIDGET, source], [CHECK, CHECK_SOURCE]]);
  const host = ts.createCompilerHost(OPTIONS);
  const { fileExists, readFile, getSourceFile, directoryExists } = host;
  host.directoryExists = (name) => name === VIRTUAL || (directoryExists?.call(host, name) ?? false);
  host.fileExists = (name) => virtual.has(name) || fileExists.call(host, name);
  host.readFile = (name) => virtual.get(name) ?? readFile.call(host, name);
  host.getSourceFile = (name, language, ...rest) =>
    virtual.has(name)
      ? ts.createSourceFile(name, /** @type {string} */ (virtual.get(name)), language, true)
      : getSourceFile.call(host, name, language, ...rest);
  const program = ts.createProgram([WIDGET, CHECK], OPTIONS, host);
  return ts.getPreEmitDiagnostics(program).map((error) => {
    const message = ts.flattenDiagnosticMessageText(error.messageText, ' ').replaceAll(`${VIRTUAL}/`, '');
    if (error.file?.fileName === WIDGET && error.start !== undefined) {
      const { line } = error.file.getLineAndCharacterOfPosition(error.start);
      return { rule: 'compile.typecheck', file: 'widget.tsx', message, line: line + 1 };
    }
    return { rule: 'compile.typecheck', file: 'widget.tsx', message: `the default export must be a Cockpit widget: ${message}` };
  });
}

const realmGlobals = {
  name: 'cockpit-realm-globals',
  setup(builder) {
    builder.onResolve({ filter: /^\.\/widget\.tsx$/ }, () => ({ path: 'widget.tsx', namespace: 'cockpit-widget' }));
    builder.onResolve({ filter: /.*/ }, (args) =>
      GLOBALS.has(args.path)
        ? { path: args.path, namespace: 'cockpit-global' }
        : { errors: [{ text: `import from '${args.path}' is not available in the realm` }] },
    );
    builder.onLoad({ filter: /.*/, namespace: 'cockpit-global' }, (args) => ({
      contents: `module.exports = globalThis.${GLOBALS.get(args.path)};`,
      loader: 'js',
    }));
  },
};

/**
 * @param {Map<string, string>} files the package
 * @param {{typecheck?: boolean}} [options] `typecheck: false` only for the adversarial corpus
 * @returns {Promise<{diagnostics: object[], bundle?: string}>}
 */
export async function compileWidget(files, { typecheck: check = true } = {}) {
  const source = files.get('widget.tsx') ?? '';
  if (check) {
    const diagnostics = typecheck(source);
    if (diagnostics.length) return { diagnostics };
  }
  const widget = {
    name: 'cockpit-widget-source',
    setup(builder) {
      builder.onLoad({ filter: /.*/, namespace: 'cockpit-widget' }, () => ({ contents: source, loader: 'tsx' }));
    },
  };
  try {
    const result = await build({
      stdin: {
        contents: "import Widget from './widget.tsx';\nglobalThis.VeetbotRealm.register(Widget);\n",
        loader: 'ts',
        resolveDir: VIRTUAL,
        sourcefile: 'entry.ts',
      },
      bundle: true,
      write: false,
      format: 'iife',
      jsx: 'automatic',
      minify: true,
      legalComments: 'none',
      target: ['es2022', 'safari17'],
      logLevel: 'silent',
      plugins: [widget, realmGlobals],
    });
    const bundle = result.outputFiles[0].text;
    if (Buffer.byteLength(bundle) > MAX_BUNDLE_BYTES) {
      return { diagnostics: [{ rule: 'compile.bundle_too_large', file: 'widget.tsx', message: `bundle exceeds ${MAX_BUNDLE_BYTES} bytes` }] };
    }
    return { diagnostics: [], bundle };
  } catch (error) {
    const failure = /** @type {{errors?: {text: string}[]}} */ (error);
    const message = failure.errors?.map((item) => item.text).join('; ') ?? String(error);
    return { diagnostics: [{ rule: 'compile.bundle', file: 'widget.tsx', message }] };
  }
}
