"""Typed application-boundary failures."""


class SessionMessageCursorError(ValueError):
    """The opaque session-message cursor could not be decoded."""


class MemoryCursorError(ValueError):
    """The opaque memory-list cursor could not be decoded."""


class SessionMetadataValidationError(ValueError):
    """Session metadata failed a service-owned boundary rule."""
