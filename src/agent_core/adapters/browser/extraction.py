"""Trusted bounded DOM reads and strict primitive conversion, without model code."""

from __future__ import annotations

import math
import re
from typing import Any

from agent_core.adapters.browser.page_structure import PAGE_TEXT_HELPERS
from agent_core.domain.browser_extraction import (
    BrowserExtractedCell,
    BrowserExtractedRow,
    BrowserExtractionRequest,
    BrowserExtractionResult,
)

EXTRACTION_SCRIPT = (
    "request => {"
    + PAGE_TEXT_HELPERS
    + """
    const openShadow = request.scope === 'main_document_and_open_shadow';
    const cellSummary = (node, limit = 256, excludeCollections = true, budget = null,
                         excludeArticles = false) =>
        summary(node, limit, excludeCollections, budget, excludeArticles, openShadow);
    const cellSelector = 'td,th,[role="cell"],[role="gridcell"]'
        + ',[role="columnheader"],[role="rowheader"]';
    const kindOf = node => {
        if (node.matches('table,[role="table"],[role="grid"]')) return 'table';
        if (node.matches('ul,ol,[role="list"]')) return 'list';
        if (node.matches('form,[role="form"]')) return 'form';
        if (node.matches('main,[role="main"]')) return 'articles';
        return null;
    };
    const visible = node => {
        const box = node.getBoundingClientRect();
        return box.width > 0 && box.height > 0 || getComputedStyle(node).display === 'contents';
    };
    let node = document.body, source = null, sourceNodes = 0, index = 0;
    while (node && sourceNodes < 8192) {
        sourceNodes++;
        let skip = blocked(node);
        if (openShadow && !skip) {
            const ancestors = ancestorExclusion(node, document.body, 8192 - sourceNodes);
            sourceNodes += ancestors.visits;
            skip = ancestors.skip;
            if (ancestors.limited) break;
        }
        if (!skip && node.nodeType === 1 && kindOf(node) === request.kind && visible(node)) {
            if (index++ === request.index) { source = node; break; }
        }
        node = advance(node, document.body, !skip, openShadow);
    }
    const result = {
        status: source ? 'extracted' : 'not_found', source_name: '', rows: [],
        source_nodes: sourceNodes, source_scan_limit_reached: !source && node !== null,
        row_nodes: 0, row_scan_limit_reached: false, omitted_rows: 0
    };
    if (!source) return result;
    result.source_name = request.kind === 'articles' ? '' : cellSummary(source).text;
    const spanning = node => {
        if (!node.matches(cellSelector))
            return false;
        return (node.rowSpan !== undefined && node.rowSpan !== 1)
            || (node.colSpan !== undefined && node.colSpan !== 1)
            || ['aria-rowspan','aria-colspan'].some(key => {
                const value = node.getAttribute(key);
                return value !== null && value !== '1';
            });
    };
    const tableCells = row => {
        let child = advance(row, row, true, openShadow), scanned = 0;
        const cells = [];
        while (child && scanned < 256 && cells.length < 16) {
            scanned++;
            let skip = blocked(child)
                || child.nodeType === 1 && child.matches(collectionSelector);
            if (openShadow && !skip) {
                const ancestors = ancestorExclusion(child, row, 256 - scanned);
                scanned += ancestors.visits;
                skip = ancestors.skip;
                if (ancestors.limited) break;
            }
            if (!skip && child.nodeType === 1 && visible(child)
                && child.matches(cellSelector))
                cells.push(child);
            child = advance(child, row, !skip, openShadow);
        }
        return {cells, truncated: child !== null && cells.length < 16};
    };
    const label = control => {
        const direct = control.getAttribute('aria-label');
        if (direct) return {text: cut(direct, 256), text_truncated: direct.length > 256};
        const rawIds = control.getAttribute('aria-labelledby') || '';
        const ids = rawIds.slice(0, 1024).trim().split(/\\s+/);
        const labels = [];
        for (const id of ids.slice(0, 8)) {
            const scope = openShadow ? control.getRootNode() : control.ownerDocument;
            const element = scope.getElementById(id);
            if (element) labels.push(element);
        }
        if (!labels.length) {
            for (let i = 0; i < Math.min(control.labels?.length || 0, 8); i++)
                labels.push(control.labels[i]);
        }
        let text = '', truncated = rawIds.length > 1024 || ids.length > 8
            || (control.labels?.length || 0) > 8;
        const budget = {remaining: 256};
        for (const element of labels) {
            // Referenced labels can sit outside the visible form. Their ancestors
            // share the cell's traversal budget and must not hide secret text.
            let ancestor = openShadow ? composedParent(element) : element.parentElement;
            let hidden = false;
            while (ancestor && budget.remaining > 0) {
                budget.remaining--;
                if (blocked(ancestor)) { hidden = true; break; }
                ancestor = openShadow ? composedParent(ancestor) : ancestor.parentElement;
            }
            if (hidden) continue;
            if (ancestor) { truncated = true; break; }
            const item = cellSummary(element, 256, true, budget);
            text += (text && item.text ? ' ' : '') + item.text;
            truncated ||= item.text_truncated || text.length > 256;
            text = cut(text, 256);
        }
        return {text, text_truncated: truncated};
    };
    const literal = value => value === null ? null : {text: String(value), text_truncated: false};
    const articleCells = article => {
        const excerpt = cellSummary(article, 256, true, null, true);
        let child = article.firstChild, scanned = 0;
        const links = new Set();
        while (child && scanned < 256) {
            scanned++;
            const skip = blocked(child) || child.nodeType === 1
                && child.matches('article,[role="article"],' + collectionSelector);
            if (!skip && child.nodeType === 1 && child.tagName === 'TIME' && visible(child)) {
                const anchor = child.closest('a[href]');
                // The link and timestamp must belong to this visible article.
                if (anchor && article.contains(anchor) && visible(anchor)) {
                    const raw = anchor.getAttribute('href') || '';
                    if (raw.length <= 256) {
                        try {
                            const url = new URL(raw, document.location.href);
                            if (url.protocol === 'https:' && url.origin === location.origin
                                && !url.username && !url.password && !url.search && !url.hash
                                && url.href.length <= 256) links.add(url.href);
                        } catch (_) { /* An invalid link supplies no evidence. */ }
                    }
                }
            }
            child = advance(child, article, !skip);
        }
        return [excerpt, child === null && links.size === 1 ? literal([...links][0]) : null];
    };
    const formCells = control => {
        const type = (control.getAttribute('type') || '').toLowerCase();
        const role = control.getAttribute('role') || (
            ['checkbox','radio'].includes(type) ? type : control.tagName === 'SELECT' ? 'combobox'
            : control.tagName === 'BUTTON' || ['submit','reset','button'].includes(type) ? 'button'
            : 'textbox');
        let checked = null;
        if (control.tagName === 'INPUT' && ['checkbox','radio'].includes(type))
            checked = control.checked;
        else if (['true','false'].includes(control.getAttribute('aria-checked')))
            checked = control.getAttribute('aria-checked') === 'true';
        return [label(control), literal(role), literal(control.matches(':disabled')
            || control.getAttribute('aria-disabled') === 'true'), literal(checked),
            literal(control.hasAttribute('required')
                || control.getAttribute('aria-required') === 'true')];
    };
    node = advance(source, source, true, openShadow);
    let matched = 0;
    while (node && result.row_nodes < 4096) {
        result.row_nodes++;
        let skip = blocked(node, request.kind === 'form')
            || request.kind !== 'articles' && node.nodeType === 1
                && node.matches(collectionSelector);
        if (openShadow && !skip) {
            const ancestors = ancestorExclusion(node, source, 4096 - result.row_nodes);
            result.row_nodes += ancestors.visits;
            skip = ancestors.skip;
            if (ancestors.limited) break;
        }
        let article = false;
        if (!skip && node.nodeType === 1 && visible(node)) {
            if (request.kind === 'table' && spanning(node)) result.status = 'unsupported_structure';
            const isRow = request.kind === 'table' ? node.matches('tr,[role="row"]')
                : request.kind === 'list' ? node.matches('li,[role="listitem"]')
                : request.kind === 'articles' ? node.matches('article,[role="article"]')
                : node.matches('input,select,textarea,button,[role="textbox"],'
                    + '[role="checkbox"],[role="radio"],[role="combobox"]')
                    || openShadow && request.kind === 'form'
                        && node.matches('[role="spinbutton"],[role="searchbox"]');
            if (isRow) {
                article = request.kind === 'articles';
                const box = article ? node.getBoundingClientRect() : null;
                if (article && (box.bottom <= 0 || box.top >= innerHeight
                    || box.right <= 0 || box.left >= innerWidth)) {
                    node = advance(node, source, false);
                    continue;
                }
                matched++;
                if (result.rows.length < request.row_limit) {
                    const table = request.kind === 'table' ? tableCells(node) : null;
                    const form = request.kind === 'form' ? formCells(node) : null;
                    const articleValues = article ? articleCells(node) : null;
                    const cells = request.fields.map(field => {
                        if (articleValues) return articleValues[field.column] || null;
                        if (form) return form[field.column] || null;
                        if (table) {
                            if (table.cells[field.column])
                                return cellSummary(table.cells[field.column]);
                            return table.truncated ? {text: '', text_truncated: true} : null;
                        }
                        return field.column === 0 ? cellSummary(node) : null;
                    });
                    result.rows.push(cells);
                    result.row_scan_limit_reached ||= table?.truncated || false;
                }
            }
        }
        // An editor's descendants are values, never additional form rows.
        const editable = node.nodeType === 1 && (node.matches('input,select,textarea')
            || openShadow && node.matches('[role="textbox"],[role="searchbox"],'
                + '[role="combobox"],[role="spinbutton"]'));
        node = advance(node, source, !skip && !editable && !article, openShadow);
    }
    result.row_scan_limit_reached ||= node !== null;
    result.omitted_rows = matched - result.rows.length;
    if (result.status === 'unsupported_structure') {
        result.rows = [];
        result.omitted_rows = matched;
    }
    return result;
}"""
)

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")


def extracted_observation(
    raw: dict[str, Any], request: BrowserExtractionRequest, revision: str
) -> BrowserExtractionResult:
    rows = []
    for row_index, source_cells in enumerate(raw["rows"]):
        cells = []
        valid = True
        for field, source in zip(request.fields, source_cells, strict=True):
            text = "" if source is None else source["text"]
            value: str | int | float | bool | None = None
            status: Any = "present"
            if source is not None and source["text_truncated"]:
                status = "truncated"
            elif not text:
                status = "missing"
            elif field.type == "string":
                value = text
            elif field.type == "boolean" and text in {"true", "false"}:
                value = text == "true"
            elif (
                field.type == "integer" and _INTEGER.fullmatch(text) and abs(int(text)) <= 2**53 - 1
            ):
                value = int(text)
            elif field.type == "number" and _NUMBER.fullmatch(text) and math.isfinite(float(text)):
                value = float(text)
            else:
                status = "invalid"
            valid &= status == "present" or (status == "missing" and not field.required)
            cells.append(
                BrowserExtractedCell(
                    field=field.name,
                    column=field.column,
                    ref=f"{revision}:cell:{row_index}:{field.column}",
                    text=text,
                    value=value,
                    status=status,
                )
            )
        rows.append(
            BrowserExtractedRow(
                ref=f"{revision}:row:{row_index}", cells=tuple(cells), schema_valid=valid
            )
        )
    result = BrowserExtractionResult(
        **{key: value for key, value in raw.items() if key != "rows"},
        revision=revision,
        kind=request.kind,
        index=request.index,
        fields=request.fields,
        row_limit=request.row_limit,
        rows=tuple(rows),
        source_ref=None if raw["status"] == "not_found" else f"{revision}:source",
        version=2 if request.scope == "main_document_and_open_shadow" else 1,
        scope="main_document_viewport_articles" if request.kind == "articles" else request.scope,
    )
    while result.rows and len(result.model_dump_json().encode("utf-8")) > 64 * 1024:
        result = result.model_copy(
            update={
                "rows": result.rows[:-1],
                "omitted_rows": result.omitted_rows + 1,
                "byte_limit_reached": True,
            }
        )
    return result
