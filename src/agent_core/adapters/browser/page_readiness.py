"""Bounded DOM stability across the document and its open shadow roots."""

from agent_core.adapters.browser.page_structure import PAGE_TRAVERSAL_HELPERS

DOM_QUIET_SCRIPT = (
    "([quietMs, timeoutMs]) => new Promise(resolve => {"
    + PAGE_TRAVERSAL_HELPERS
    + """
    const deadline = performance.now() + timeoutMs;
    const options = {subtree: true, childList: true, attributes: true, characterData: true};
    let done = false, quiet = 0, limit = 0, poll = 0, total = 0;
    let roots = [document], changedAt = performance.now();
    const observer = new MutationObserver(changed);

    function finish(quietReached) {
        if (done) return;
        done = true;
        observer.disconnect();
        clearTimeout(quiet);
        clearTimeout(limit);
        clearInterval(poll);
        roots = [];
        resolve(quietReached);
    }
    function scheduleQuiet() {
        clearTimeout(quiet);
        quiet = setTimeout(checkQuiet, Math.max(1, quietMs - (performance.now() - changedAt)));
    }
    function changed() {
        if (done) return;
        changedAt = performance.now();
        scheduleQuiet();
    }
    function discover() {
        if (done) return false;
        if (performance.now() >= deadline) { finish(false); return false; }
        try {
            const found = [document];
            let node = document.documentElement, visited = 0;
            while (node) {
                if (++visited > 8192 || ++total > 65536 || performance.now() >= deadline) {
                    finish(false); return false;
                }
                if (node.shadowRoot) {
                    if (found.length === 65) { finish(false); return false; }
                    found.push(node.shadowRoot);
                }
                node = advance(node, document, true, true);
            }
            // Drain queued mutations before changing subscriptions or claiming quiet.
            if (observer.takeRecords().length) changed();
            if (found.length !== roots.length || found.some((root, i) => root !== roots[i])) {
                observer.disconnect();
                for (const root of found) observer.observe(root, options);
                roots = found;
                changed();
            }
            return true;
        } catch {
            finish(false); return false;
        }
    }
    function checkQuiet() {
        // attachShadow on an existing host is not a document mutation. Recheck
        // roots at the quiet boundary as well as during the bounded polling.
        if (!discover()) return;
        if (performance.now() >= deadline) finish(false);
        else if (performance.now() - changedAt >= quietMs) finish(true);
        else scheduleQuiet();
    }
    observer.observe(document, options);
    limit = setTimeout(() => finish(false), timeoutMs);
    if (discover()) {
        scheduleQuiet();
        poll = setInterval(discover, 100);
    }
})"""
)
