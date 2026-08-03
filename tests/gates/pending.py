"""Resolvable target for gates whose owning milestone has not started."""


def pending_gate() -> None:
    """Represent a declared gate without reporting it as passed or skipped."""

    raise RuntimeError("the owning milestone has not implemented this gate")
