"""Bounded challenge detection; never collect credentials or frame contents."""

import json

from agent_core.adapters.browser.page_structure import PAGE_TEXT_HELPERS

# Shared with ceremony verification: a remembered account or an interstitial
# must never become positive authentication evidence (ADR-0316).
SECURITY_CHALLENGE_TEXT = (
    r"^\s*(?:performing\s+security\s+verification|verify\s+you\s+are\s+human)\s*[.!…]?\s*$"
)
# textContent joins an icon label and a following <br> without whitespace.
REMEMBERED_SIGN_IN_TEXT = r"sign\s+in\s+as\s+\S"
SECURITY_CHALLENGE_HEADINGS = "h1,h2,h3,h4,h5,h6,[role=heading]"
REMEMBERED_SIGN_IN_CONTROLS = "a,button,[role=link],[role=button]"

NAMED_CHALLENGE_SCRIPT = (
    "(node, pattern) => {"
    + PAGE_TEXT_HELPERS
    + """
    const value = summary(node, 512, false, null, false, true);
    return !value.text_truncated && new RegExp(pattern, 'i').test(value.text);
}"""
)

CREDENTIAL_VISIBLE_SCRIPT = (
    "node => {"
    + PAGE_TEXT_HELPERS
    + """
    let parent = node, scanned = 0;
    while (parent && scanned++ < 256) {
        const style = getComputedStyle(parent);
        if (parent.hidden || parent.getAttribute('aria-hidden') === 'true'
            || style.display === 'none' || style.visibility !== 'visible'
            || style.opacity === '0') return false;
        parent = composedParent(parent);
    }
    if (parent) return true; // Incomplete ancestry cannot authorize automation.
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
}"""
)

AUTHENTICATION_SCRIPT = (
    "() => {"
    + PAGE_TEXT_HELPERS
    + f"const securityText = new RegExp({json.dumps(SECURITY_CHALLENGE_TEXT)}, 'i');"
    + f"const rememberedText = new RegExp({json.dumps(REMEMBERED_SIGN_IN_TEXT)}, 'i');"
    + f"const securityHeadings = {json.dumps(SECURITY_CHALLENGE_HEADINGS)};"
    + f"const rememberedControls = {json.dumps(REMEMBERED_SIGN_IN_CONTROLS)};"
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
        let namedChallenge = false;
        if (element && (node.matches(securityHeadings) || node.matches(rememberedControls))) {
            const budget = {remaining: 8192 - scanned};
            const value = summary(node, 512, false, budget, false, true);
            scanned = 8192 - budget.remaining;
            namedChallenge = !value.text_truncated && (
                node.matches(securityHeadings) && securityText.test(value.text)
                || node.matches(rememberedControls) && rememberedText.test(value.text));
        }
        let context = null;
        if (credential || captcha || namedChallenge || node === root || node.assignedSlot) {
            context = ancestry(node);
            if (!context) break;
        }
        if (context && !context.hidden
            && (credential || captcha && !context.badge || namedChallenge && !context.editable)) {
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
