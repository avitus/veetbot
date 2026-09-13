"""Typed application-boundary failures."""


class SessionMessageCursorError(ValueError):
    """The opaque session-message cursor could not be decoded."""


class MemoryCursorError(ValueError):
    """The opaque memory-list cursor could not be decoded."""


class SessionMetadataValidationError(ValueError):
    """Session metadata failed a service-owned boundary rule."""


class BrowserLoginURLValidationError(ValueError):
    """A login URL is invalid or outside the owned profile's exact origins."""


class EmailFeedbackTargetError(ValueError):
    """Owner feedback does not identify one supported person or content topic."""
