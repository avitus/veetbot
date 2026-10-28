"""Which same-origin external SVG ``<use>`` copies are inert (ADR-0263).

Page script cannot see the copy an external ``<use>`` reference renders, so
ADR-0129 treats its element as reaching a target outside every origin.
Chromium's DevTools protocol shows the copy as the element's user-agent shadow
root, outside the page's script realm. This module reads that document tree
and returns the resolved references every one of whose copies is loaded and
holds nothing a click could follow or that could change later.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

# Animation can point a <use> at another reference after it was read, so the
# copy DevTools shows would not be the one its attribute names.
_ANIMATION_ELEMENTS = frozenset(
    {"set", "animate", "animateMotion", "animateTransform", "animateColor", "discard"}
)
# Elements a copy must not hold: a link, an embedded document, and HTML
# through a foreign object, as the page-side read refuses; and animation.
# Chromium already leaves some of these out of a copy; the check does not
# depend on it.
_UNSAFE_ELEMENTS = (
    frozenset({"a", "iframe", "frame", "object", "embed", "foreignObject"}) | _ANIMATION_ELEMENTS
)
# The page-side read's walk bound, applied to each copy.
MAXIMUM_COPY_NODES = 2048
_ELEMENT_NODE = 1
_USER_AGENT = "user-agent"


def inspected_use_references(document: Mapping[str, Any]) -> frozenset[str]:
    """Resolved same-origin external references whose every copy is inert.

    ``document`` is the root of ``DOM.getDocument`` with ``pierce`` and full
    depth. One reference names one copy within a document, so each ``<use>``
    naming a single reference witnesses its copy. A reference qualifies when it
    is on the document's origin and every such witness has a loaded copy that
    passes ``_copy_is_inert``. A ``<use>`` naming two references witnesses
    neither, since either could be the one rendered; the page-side read still
    requires both to qualify. A page with any animation element inspects
    nothing. Frames' documents are not walked: their references resolve
    elsewhere, and the page-side read treats a frame as opaque.
    """

    base = str(document.get("baseURL") or document.get("documentURL") or "")
    origin = _origin(str(document.get("documentURL") or ""))
    if origin is None:
        return frozenset()
    nodes = list(_light_nodes(document))
    if any(node.get("localName") in _ANIMATION_ELEMENTS for node in nodes):
        return frozenset()
    inert: set[str] = set()
    refused: set[str] = set()
    for node in nodes:
        if node.get("localName") != "use":
            continue
        values = {value.strip() for value in _references(node) if value.strip()}
        if len(values) != 1:
            continue
        [value] = values
        if value.startswith("#"):
            continue
        reference = urljoin(base, value)
        if _origin(reference) == origin and _copy_is_inert(node):
            inert.add(reference)
        else:
            refused.add(reference)
    return frozenset(inert - refused)


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".casefold()


def _attributes(node: Mapping[str, Any]) -> dict[str, str]:
    flat = node.get("attributes") or []
    return {str(flat[index]): str(flat[index + 1]) for index in range(0, len(flat) - 1, 2)}


def _references(node: Mapping[str, Any]) -> list[str]:
    attributes = _attributes(node)
    return [attributes[name] for name in ("href", "xlink:href") if name in attributes]


def _light_nodes(document: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """The document's nodes and its author shadow trees, not copies or frames."""

    pending = list(document.get("children") or [])
    while pending:
        node = pending.pop()
        yield node
        pending.extend(node.get("children") or [])
        pending.extend(
            root
            for root in node.get("shadowRoots") or []
            if root.get("shadowRootType") != _USER_AGENT
        )


def _copy_of(node: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for root in node.get("shadowRoots") or []:
        if isinstance(root, Mapping) and root.get("shadowRootType") == _USER_AGENT:
            return cast(Mapping[str, Any], root)
    return None


def _copy_is_inert(use: Mapping[str, Any]) -> bool:
    """A loaded copy, and every nested copy, holds no unsafe element or image map."""

    copy = _copy_of(use)
    if copy is None or not copy.get("children"):
        return False
    pending = list(copy.get("children") or [])
    seen = 0
    while pending:
        node = pending.pop()
        seen += 1
        if seen > MAXIMUM_COPY_NODES:
            return False
        if node.get("nodeType") == _ELEMENT_NODE:
            if node.get("localName") in _UNSAFE_ELEMENTS or "usemap" in _attributes(node):
                return False
            if node.get("localName") == "use" and _references(node):
                nested = _copy_of(node)
                if nested is None or not nested.get("children"):
                    return False
                pending.extend(nested.get("children") or [])
        if node.get("contentDocument") is not None:
            return False
        pending.extend(node.get("children") or [])
    return True
