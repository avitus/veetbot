"""Typed application-boundary failures."""


class SessionMessageCursorError(ValueError):
    """The opaque session-message cursor could not be decoded."""
