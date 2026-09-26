"""Errors raised at the HTTP boundary by routes and flag-mounted routers."""


class MalformedRequestError(ValueError):
    """A syntactically invalid value detected at the HTTP boundary."""
