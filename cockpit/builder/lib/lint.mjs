// Source hygiene for widget code. These rules give the author fast, specific
// feedback; they are not the security boundary, which is the network-less realm.
import ts from 'typescript';

const ALLOWED_IMPORTS = new Set(['react', '@veetbot/cockpit-kit']);
const GLOBAL_OBJECTS = new Set(['window', 'globalThis', 'self', 'frames']);
const BANNED_GLOBALS = new Set([
  'fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'RTCPeerConnection', 'webkitRTCPeerConnection',
  'RTCDataChannel', 'eval', 'Function', 'parent', 'top', 'opener', 'location', 'open', 'postMessage',
  'localStorage', 'sessionStorage', 'indexedDB', 'caches', 'cookieStore', 'require', 'importScripts',
  'Worker', 'SharedWorker',
]);
const BANNED_MEMBERS = new Set(['innerHTML', 'outerHTML', 'insertAdjacentHTML']);
const BANNED_RECEIVER_MEMBERS = new Map([
  ['document', new Set(['cookie', 'write', 'writeln', 'domain', 'location'])],
  ['navigator', new Set(['sendBeacon', 'serviceWorker'])],
]);
const BANNED_ELEMENTS = new Set(['script', 'iframe', 'frame', 'object', 'embed', 'link', 'meta', 'base', 'form', 'a', 'portal']);
const COLOUR = /#[0-9a-f]{3}(?:[0-9a-f]{1,5})?\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(/i;

function isDeclarationName(node) {
  const parent = node.parent;
  if (!parent) return false;
  if ('name' in parent && parent.name === node) {
    return !ts.isPropertyAccessExpression(parent) && !ts.isShorthandPropertyAssignment(parent);
  }
  if (ts.isBindingElement(parent) && parent.propertyName === node) return true;
  if (ts.isImportSpecifier(parent) || ts.isExportSpecifier(parent) || ts.isImportClause(parent)) return true;
  if (ts.isQualifiedName(parent) || ts.isTypeReferenceNode(parent)) return true;
  if (ts.isJsxAttribute(parent) || ts.isLabeledStatement(parent) || ts.isBreakOrContinueStatement(parent)) return true;
  return false;
}

function declaredNames(sourceFile) {
  const names = new Set();
  const visit = (node) => {
    if (ts.isIdentifier(node) && isDeclarationName(node) && !ts.isPropertyAssignment(node.parent)) names.add(node.text);
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return names;
}

/**
 * Lint one widget component source.
 * @param {string} source the contents of `widget.tsx`
 * @returns {{rule: string, file: string, message: string, line?: number}[]}
 */
export function lintSource(source) {
  const sourceFile = ts.createSourceFile('widget.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const parseErrors = /** @type {{parseDiagnostics?: ts.Diagnostic[]}} */ (/** @type {unknown} */ (sourceFile)).parseDiagnostics ?? [];
  if (parseErrors.length) {
    const message = ts.flattenDiagnosticMessageText(parseErrors[0].messageText, ' ');
    return [{ rule: 'lint.syntax', file: 'widget.tsx', message }];
  }
  const local = declaredNames(sourceFile);
  const diagnostics = [];
  let hasDefaultExport = false;
  const report = (rule, node, message) => {
    const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
    diagnostics.push({ rule, file: 'widget.tsx', message, line: line + 1 });
  };

  const visit = (node) => {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) {
      const module = /** @type {ts.StringLiteral} */ (node.moduleSpecifier).text;
      if (!ALLOWED_IMPORTS.has(module)) report('lint.import', node, `import from '${module}' is not allowed`);
    }
    if (ts.isExportAssignment(node) && !node.isExportEquals) hasDefaultExport = true;
    if (
      (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node)) &&
      node.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword)
    ) {
      hasDefaultExport = true;
    }
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      report('lint.dynamic_import', node, 'dynamic import() is not allowed');
    }
    if (ts.isElementAccessExpression(node) && ts.isIdentifier(node.expression) && GLOBAL_OBJECTS.has(node.expression.text)) {
      report('lint.computed_global', node, `computed access on ${node.expression.text} is not allowed`);
    }
    if (ts.isPropertyAccessExpression(node)) {
      const name = node.name.text;
      const receiver = ts.isIdentifier(node.expression) ? node.expression.text : undefined;
      if (
        (receiver && GLOBAL_OBJECTS.has(receiver) && BANNED_GLOBALS.has(name)) ||
        BANNED_MEMBERS.has(name) ||
        (receiver && BANNED_RECEIVER_MEMBERS.get(receiver)?.has(name))
      ) {
        report('lint.banned', node, `${receiver ? `${receiver}.` : ''}${name} is not allowed`);
      }
    }
    if (
      ts.isIdentifier(node) &&
      BANNED_GLOBALS.has(node.text) &&
      !local.has(node.text) &&
      !isDeclarationName(node) &&
      !(ts.isPropertyAccessExpression(node.parent) && node.parent.name === node)
    ) {
      report('lint.banned', node, `${node.text} is not allowed`);
    }
    if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer && ts.isIdentifier(node.initializer) && GLOBAL_OBJECTS.has(node.initializer.text)) {
      for (const element of node.name.elements) {
        const key = element.propertyName ?? element.name;
        if (ts.isIdentifier(key) && BANNED_GLOBALS.has(key.text)) report('lint.banned', element, `${key.text} is not allowed`);
      }
    }
    if ((ts.isJsxAttribute(node) || ts.isPropertyAssignment(node)) && node.name.getText(sourceFile) === 'dangerouslySetInnerHTML') {
      report('lint.banned', node, 'dangerouslySetInnerHTML is not allowed');
    }
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && ts.isIdentifier(node.tagName) && BANNED_ELEMENTS.has(node.tagName.text)) {
      report('lint.element', node, `<${node.tagName.text}> is not allowed`);
    }
    if ((ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) && !ts.isImportDeclaration(node.parent) && !ts.isExportDeclaration(node.parent) && COLOUR.test(node.text)) {
      report('lint.colour_literal', node, 'use kit colour tokens instead of colour literals');
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  if (!hasDefaultExport) {
    diagnostics.push({ rule: 'lint.default_export', file: 'widget.tsx', message: 'the component must be the default export' });
  }
  return diagnostics;
}
