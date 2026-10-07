"""Bounded challenge detection; never collect credentials or frame contents."""

from agent_core.adapters.browser.page_structure import PAGE_TEXT_HELPERS

AUTHENTICATION_SCRIPT = (
    "() => {"
    + PAGE_TEXT_HELPERS
    + """
    let node = document.body, scanned = 0;
    while (node && scanned++ < 8192) {
        const element = node.nodeType === 1;
        const credential = element && node.matches(
            'input[type="password"],input[autocomplete="one-time-code"]');
        const captcha = element && !node.closest('.grecaptcha-badge') && node.matches(
            'iframe[src*="captcha" i],iframe[title*="captcha" i],'
            + '[id*="captcha" i],[class*="captcha" i]');
        const challenge = credential || captcha;
        if (challenge) {
            const box = node.getBoundingClientRect(), style = getComputedStyle(node);
            if (box.width > 0 && box.height > 0 && style.visibility === 'visible'
                && style.opacity !== '0' && !node.closest('[hidden],[aria-hidden="true"]'))
                return true;
        }
        const skip = blocked(node, true);
        if (!skip && node.nodeType === 3 && node.parentElement?.closest(
            'form,dialog,[role="dialog"],[role="alertdialog"],button')) {
            const value = cut(node.nodeValue || '', 512);
            if (/use\\s+(?:a\\s+)?passkey|verification\\s+code/i.test(value)
                || /multi-factor|two-factor|consent\\s+required/i.test(value))
                return true;
        }
        node = advance(node, document.body, !skip);
    }
    return false;
}"""
)
