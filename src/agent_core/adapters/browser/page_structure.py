"""Bounded trusted collection of visible main-document semantic evidence."""

# Do not use innerText/textContent: those materialize unbounded subtrees and can
# include editable values. The two walks have independent node/text ceilings.
PAGE_TEXT_HELPERS = """
    const collectionSelector = 'table,ul,ol,form,[role="table"],[role="grid"]'
        + ',[role="list"],[role="form"]';
    const cut = (value, limit) => {
        const part = value.slice(0, limit);
        const last = part.charCodeAt(part.length - 1);
        return last >= 0xD800 && last <= 0xDBFF ? part.slice(0, -1) : part;
    };
    const blocked = (node, includeControls = false) => {
        if (node.nodeType !== 1) return false;
        if (['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','IFRAME','OBJECT','EMBED']
            .includes(node.tagName)) return true;
        if (!includeControls && (['INPUT','TEXTAREA','SELECT'].includes(node.tagName)
            || node.matches('[role="textbox"],[role="searchbox"],'
                + '[role="combobox"],[role="spinbutton"]'))) return true;
        if (node.isContentEditable || node.hidden || node.getAttribute('aria-hidden') === 'true')
            return true;
        const style = getComputedStyle(node);
        return style.display === 'none' || style.visibility !== 'visible' || style.opacity === '0';
    };
    // One sibling per level, never an array of every child of a hostile node.
    const advance = (node, root, descend) => {
        if (descend && node.firstChild) return node.firstChild;
        while (node && node !== root) {
            if (node.nextSibling) return node.nextSibling;
            node = node.parentNode;
        }
        return null;
    };
    const summary = (root, limit = 512, excludeCollections = false, budget = null) => {
        let node = root, scanned = 0, text = '', clipped = false;
        const nodeLimit = budget ? Math.min(256, budget.remaining) : 256;
        while (node && scanned < nodeLimit && text.length < limit) {
            scanned++;
            const skip = blocked(node) || (excludeCollections && node !== root
                && node.nodeType === 1 && node.matches(collectionSelector));
            if (!skip && node.nodeType === 3) {
                const range = document.createRange();
                range.selectNode(node);
                const box = range.getBoundingClientRect();
                if (box.width > 0 && box.height > 0) {
                    const remaining = limit - text.length;
                    const value = node.nodeValue || '';
                    const part = cut(value, remaining + 1).replace(/\\s+/g, ' ').trim();
                    const addition = (text && part ? ' ' : '') + part;
                    clipped ||= value.length > remaining || addition.length > remaining;
                    text += cut(addition, remaining);
                }
            }
            node = advance(node, root, !skip);
        }
        if (budget) budget.remaining -= scanned;
        return {text, text_truncated: clipped || node !== null};
    };
"""

REGION_SCRIPT = (
    "root => {"
    + PAGE_TEXT_HELPERS
    + """
    const priority = {dialog: 0, alert: 1, status: 2, form: 3, main: 4, section: 5, heading: 6};
    root ||= document.body;
    const kindOf = node => {
        const role = node.getAttribute('role');
        if (node.tagName === 'DIALOG' || role === 'dialog' || role === 'alertdialog')
            return 'dialog';
        if (role === 'alert') return 'alert';
        if (role === 'status') return 'status';
        if (node.tagName === 'FORM' || role === 'form') return 'form';
        if (node.tagName === 'MAIN' || role === 'main') return 'main';
        if (node.tagName === 'SECTION' || role === 'region') return 'section';
        if (/^H[1-6]$/.test(node.tagName) || role === 'heading') return 'heading';
        return null;
    };
    const regions = [];
    let node = root, scanned = 0, matched = 0;
    while (node && scanned < 8192) {
        scanned++;
        const skip = blocked(node);
        if (!skip && node.nodeType === 1) {
            const kind = kindOf(node);
            const box = kind ? node.getBoundingClientRect() : null;
            if (kind && (box.width > 0 && box.height > 0
                         || getComputedStyle(node).display === 'contents')) {
                matched++;
                // Read at most 32 summaries, after selecting the strongest evidence.
                regions.push({kind, node, order: scanned});
                regions.sort((a,b) => priority[a.kind] - priority[b.kind] || a.order - b.order);
                if (regions.length > 32) regions.pop();
            }
        }
        node = advance(node, root, !skip);
    }
    return {
        regions: regions.map(region => ({kind: region.kind, ...summary(region.node)})),
        coverage: {version: 1, scope: 'main_document', scanned_nodes: scanned,
                   scan_limit_reached: node !== null, omitted_regions: matched - regions.length}
    };
}"""
)

# The node array stays inside the trusted runtime, never in a serialized result.
REGION_CAPTURE_SCRIPT = REGION_SCRIPT.replace(
    "regions: regions.map", "nodes: regions.map(region => region.node), regions: regions.map"
)
