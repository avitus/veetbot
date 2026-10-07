"""Confirm only previously submitted draft text; never return editable page contents."""

CONFIRM_DRAFT_SCRIPT = """(weak, [expected, root]) => {
    const node = weak.deref();
    if (!node || !node.isConnected || node.ownerDocument !== document
        || !node.isContentEditable) return false;
    let parent = node, ancestors = 0;
    while (parent) {
        if (++ancestors > 256) return false;
        if (parent.nodeType === 1) {
            if (parent.hidden || parent.getAttribute('aria-hidden') === 'true'
                || parent.matches('input,textarea,select,iframe,object,embed')
                || ['current-password','new-password','one-time-code']
                    .includes(parent.getAttribute('autocomplete'))) return false;
            const style = getComputedStyle(parent);
            if (style.display === 'none' || style.visibility !== 'visible'
                || style.opacity === '0') return false;
        }
        parent = parent.assignedSlot || parent.parentNode;
        if (parent?.nodeType === 11) parent = parent.host;
    }
    const box = node.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return false;
    // Prove this subtree small before asking for its rendered representation.
    // Only an equality boolean crosses the browser boundary, never its text.
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_ALL);
    let current = node, visited = 0, characters = 0;
    while (current) {
        if (++visited > 256) return false;
        if (current.nodeType === 1 && (current.shadowRoot
            || current.matches('input,textarea,select,iframe,object,embed,slot'))) return false;
        if (current.nodeType === 3) characters += current.nodeValue.length;
        if (characters > 4096) return false;
        current = walker.nextNode();
    }
    if (node.innerText !== expected) return false;
    // A valid receipt outside this observation's scope remains available later.
    return root && !root.contains(node) ? null : true;
}"""
