"""Bounded challenge detection; never collect credentials or frame contents."""

from agent_core.adapters.browser.page_structure import PAGE_TEXT_HELPERS

AUTHENTICATION_SCRIPT = (
    "() => {"
    + PAGE_TEXT_HELPERS
    + """
    const root = document.body;
    let node = root, scanned = 0;
    const ancestry = node => {
        let hidden = false, editable = false, badge = false, prompt = false;
        let parent = node.nodeType === 1 ? node : composedParent(node);
        while (parent) {
            if (scanned === 8192) return null;
            scanned++;
            const style = getComputedStyle(parent);
            hidden ||= parent.hidden || parent.getAttribute('aria-hidden') === 'true'
                || style.display === 'none' || style.visibility !== 'visible'
                || style.opacity === '0';
            editable ||= parent.isContentEditable || parent.matches(
                'input,textarea,select,[role="textbox"],[role="searchbox"],'
                + '[role="combobox"],[role="spinbutton"]');
            badge ||= parent.matches('.grecaptcha-badge');
            prompt ||= parent.matches('form,dialog,button,[role="form"],[role="dialog"],'
                + '[role="alertdialog"],[role="button"]');
            parent = composedParent(parent);
        }
        return {hidden, editable, badge, prompt};
    };
    while (node && scanned < 8192) {
        scanned++;
        const element = node.nodeType === 1;
        const credential = element && node.matches(
            'input[type="password"],input[autocomplete~="one-time-code" i]');
        const captcha = element && node.matches(
            'iframe[src*="captcha" i],iframe[title*="captcha" i],'
            + '[id*="captcha" i],[class*="captcha" i]');
        let context = null;
        if (credential || captcha || node === root || node.assignedSlot) {
            context = ancestry(node);
            if (!context) break;
        }
        if (context && !context.hidden
            && (credential || captcha && !context.badge)) {
            const box = node.getBoundingClientRect();
            if (box.width > 0 && box.height > 0)
                return true;
        }
        const skip = context?.hidden || blocked(node, true);
        if (!skip && node.nodeType === 3) {
            context ||= ancestry(node);
            if (!context) break;
            if (!context.hidden && !context.editable && context.prompt) {
                const value = cut(node.nodeValue || '', 512);
                if (/use\\s+(?:a\\s+)?passkey|verification\\s+code/i.test(value)
                    || /multi-factor|two-factor|consent\\s+required/i.test(value)) {
                    const range = document.createRange();
                    range.selectNode(node);
                    const box = range.getBoundingClientRect();
                    if (box.width > 0 && box.height > 0) return true;
                }
            }
        }
        node = advance(node, root, !skip, true);
    }
    return false;
}"""
)
