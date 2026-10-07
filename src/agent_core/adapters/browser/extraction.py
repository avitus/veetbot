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
    const cellSelector = 'td,th,[role="cell"],[role="gridcell"]'
        + ',[role="columnheader"],[role="rowheader"]';
    const kindOf = node => {
        if (node.matches('table,[role="table"],[role="grid"]')) return 'table';
        if (node.matches('ul,ol,[role="list"]')) return 'list';
        if (node.matches('form,[role="form"]')) return 'form';
        return null;
    };
    const visible = node => {
        const box = node.getBoundingClientRect();
        return box.width > 0 && box.height > 0 || getComputedStyle(node).display === 'contents';
    };
    let node = document.body, source = null, sourceNodes = 0, index = 0;
    while (node && sourceNodes < 8192) {
        sourceNodes++;
        const skip = blocked(node);
        if (!skip && node.nodeType === 1 && kindOf(node) === request.kind && visible(node)) {
            if (index++ === request.index) { source = node; break; }
        }
        node = advance(node, document.body, !skip);
    }
    const result = {
        status: source ? 'extracted' : 'not_found', source_name: '', rows: [],
        source_nodes: sourceNodes, source_scan_limit_reached: !source && node !== null,
        row_nodes: 0, row_scan_limit_reached: false, omitted_rows: 0
    };
    if (!source) return result;
    result.source_name = summary(source, 256, true).text;
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
        let child = row.firstChild, scanned = 0;
        const cells = [];
        while (child && scanned < 256 && cells.length < 16) {
            scanned++;
            const skip = blocked(child)
                || child.nodeType === 1 && child.matches(collectionSelector);
            if (!skip && child.nodeType === 1 && visible(child)
                && child.matches(cellSelector))
                cells.push(child);
            child = advance(child, row, !skip);
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
            const element = control.ownerDocument.getElementById(id);
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
            let ancestor = element.parentElement, hidden = false;
            while (ancestor && budget.remaining > 0) {
                budget.remaining--;
                if (blocked(ancestor)) { hidden = true; break; }
                ancestor = ancestor.parentElement;
            }
            if (hidden) continue;
            if (ancestor) { truncated = true; break; }
            const item = summary(element, 256, true, budget);
            text += (text && item.text ? ' ' : '') + item.text;
            truncated ||= item.text_truncated || text.length > 256;
            text = cut(text, 256);
        }
        return {text, text_truncated: truncated};
    };
    const literal = value => value === null ? null : {text: String(value), text_truncated: false};
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
    node = source.firstChild;
    let matched = 0;
    while (node && result.row_nodes < 4096) {
        result.row_nodes++;
        const skip = blocked(node, request.kind === 'form')
            || node.nodeType === 1 && node.matches(collectionSelector);
        if (!skip && node.nodeType === 1 && visible(node)) {
            if (request.kind === 'table' && spanning(node)) result.status = 'unsupported_structure';
            const isRow = request.kind === 'table' ? node.matches('tr,[role="row"]')
                : request.kind === 'list' ? node.matches('li,[role="listitem"]')
                : node.matches('input,select,textarea,button,[role="textbox"],'
                    + '[role="checkbox"],[role="radio"],[role="combobox"]');
            if (isRow) {
                matched++;
                if (result.rows.length < request.row_limit) {
                    const table = request.kind === 'table' ? tableCells(node) : null;
                    const form = request.kind === 'form' ? formCells(node) : null;
                    const cells = request.fields.map(field => {
                        if (form) return form[field.column] || null;
                        if (table) {
                            if (table.cells[field.column])
                                return summary(table.cells[field.column], 256, true);
                            return table.truncated ? {text: '', text_truncated: true} : null;
                        }
                        return field.column === 0 ? summary(node, 256, true) : null;
                    });
                    result.rows.push(cells);
                    result.row_scan_limit_reached ||= table?.truncated || false;
                }
            }
        }
        // Native input/select/textarea descendants are values, never new rows.
        const editable = node.nodeType === 1 && node.matches('input,select,textarea');
        node = advance(node, source, !skip && !editable);
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
