"""Bound visible prose before transfer, keeping editable state out of page text."""

from agent_core.adapters.browser.page_structure import PAGE_TEXT_HELPERS

READABLE_TEXT_SCRIPT = (
    "root => {"
    + PAGE_TEXT_HELPERS
    + """
    root ||= document.body;
    const maxNodes = 8192, maxUnits = 262144, maxBytes = 262144;
    const encoder = new TextEncoder();
    const blocks = new WeakSet();
    let text = '', bytes = 0, separator = '', nodes = 0, units = 0;
    let textLimited = false, nodeLimited = false, node = root;
    const excluded = current => blocked(current) || current.nodeType === 1
        && current.matches('[role="textbox"],[role="searchbox"],'
            + '[role="combobox"],[role="spinbutton"]');
    if (document.documentElement) {
        nodes++;
        if (excluded(document.documentElement)) node = null;
    }
    const boundary = value => {
        if (separator !== '\\n') separator = value;
    };
    const next = (current, descend) => {
        if (descend) {
            if (current.shadowRoot?.firstChild) return current.shadowRoot.firstChild;
            if (current.firstChild) return current.firstChild;
        }
        while (current && current !== root) {
            if (blocks.has(current)) boundary('\\n');
            if (current.nextSibling) return current.nextSibling;
            const parent = current.parentNode;
            if (parent?.nodeType === 11 && parent.host) {
                current = parent.host;
                if (current.firstChild) return current.firstChild;
            } else current = parent;
        }
        return null;
    };
    const append = value => {
        let addition = value.replace(/\\s+/g, ' ');
        if (!text || separator || text.endsWith(' ')) addition = addition.replace(/^ +/, '');
        if (!addition) return;
        if (separator && text) {
            const trimmed = text.replace(/ +$/, '');
            bytes -= text.length - trimmed.length;
            text = trimmed;
            addition = separator + addition;
        }
        separator = '';
        const available = maxBytes - bytes;
        if (encoder.encode(addition).length > available) {
            let low = 0, high = addition.length;
            while (low < high) {
                const middle = Math.ceil((low + high) / 2);
                if (encoder.encode(cut(addition, middle)).length <= available) low = middle;
                else high = middle - 1;
            }
            addition = cut(addition, low);
            textLimited = true;
        }
        text += addition;
        bytes += encoder.encode(addition).length;
    };
    while (node && nodes < maxNodes && !textLimited) {
        nodes++;
        let skip = excluded(node);
        // A light-DOM node can be rendered beneath a hidden/editable shadow
        // ancestor. Check that composed ancestry within the same visit budget.
        let parent = skip ? null : node.assignedSlot;
        while (parent && parent !== document.body) {
            if (nodes >= maxNodes) { nodeLimited = true; skip = true; break; }
            nodes++;
            if (excluded(parent)) { skip = true; break; }
            parent = parent.assignedSlot || parent.parentNode;
            if (parent?.nodeType === 11) parent = parent.host;
        }
        if (nodeLimited) break;
        if (!skip && node.nodeType === 1) {
            const display = getComputedStyle(node).display;
            if (node.tagName === 'BR') boundary('\\n');
            else if (display === 'table-cell') boundary(' ');
            else if (!['inline','inline-block','inline-flex','inline-grid','contents']
                     .includes(display)) {
                blocks.add(node);
                boundary('\\n');
            }
        }
        if (!skip && node.nodeType === 3) {
            const range = document.createRange();
            range.selectNode(node);
            const box = range.getBoundingClientRect();
            if (box.width > 0 && box.height > 0) {
                const available = maxUnits - units;
                const value = node.nodeValue || '';
                const part = cut(value, available);
                units += part.length;
                append(part);
                textLimited ||= value.length > available;
            }
        }
        node = next(node, !skip);
    }
    return {
        text: text.trim(),
        coverage: {
            version: 1, scope: 'main_document_and_open_shadow', scanned_nodes: nodes,
            scanned_text_characters: units,
            node_limit_reached: nodeLimited || node !== null && nodes >= maxNodes,
            text_limit_reached: textLimited, omitted_text_bytes: 0
        }
    };
}"""
)
