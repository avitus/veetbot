"""ADR-0258: which external ``<use>`` copies a DevTools document read shows inert.

Real Chromium covers the rendered cases in the hostile-construct suite; these
synthetic ``DOM.getDocument`` trees cover what Chromium leaves out of a copy
and the walk's own bounds.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.adapters.browser.use_copies import (
    MAXIMUM_COPY_NODES,
    inspected_use_references,
)

PAGE = "https://site.test/lesson/1"
SPRITE = "https://site.test/lesson/icons.svg"


def element(name: str, *children: dict[str, Any], **attributes: str) -> dict[str, Any]:
    flat = [part for pair in attributes.items() for part in pair]
    return {"nodeType": 1, "localName": name, "attributes": flat, "children": list(children)}


def copy(*children: dict[str, Any]) -> dict[str, Any]:
    return {"nodeType": 11, "shadowRootType": "user-agent", "children": list(children)}


def use(reference: str, *copied: dict[str, Any], loaded: bool = True) -> dict[str, Any]:
    node = element("use", href=reference)
    node["shadowRoots"] = [copy(*copied)] if loaded else []
    return node


def icon() -> dict[str, Any]:
    return element("symbol", element("path", d="M0 0h10v10z"))


def document(*body: dict[str, Any], url: str = PAGE) -> dict[str, Any]:
    return {
        "nodeType": 9,
        "documentURL": url,
        "baseURL": url,
        "children": [element("html", element("body", *body))],
    }


def test_an_inert_copy_through_nested_copies_is_inspected() -> None:
    nested = element("use", **{"xlink:href": "#_base"})
    nested["shadowRoots"] = [copy(icon())]
    tree = document(use("/lesson/icons.svg#menu", element("symbol", nested)))

    assert inspected_use_references(tree) == frozenset({f"{SPRITE}#menu"})


@pytest.mark.parametrize(
    "inside",
    [
        element("a", element("rect"), href="/courses/remove-course"),
        element("foreignObject", element("iframe", src="/courses/remove-course")),
        element("object"),
        element("embed"),
        element("image", usemap="#map"),
        element("set", attributeName="href", to="#hidden"),
        element("animate"),
        element("use", href="#_base"),  # a nested copy that has not loaded
    ],
    ids=["link", "foreign_object", "object", "embed", "image_map", "set", "animate", "unloaded"],
)
def test_a_copy_holding_anything_unsafe_is_not_inspected(inside: dict[str, Any]) -> None:
    tree = document(use("/lesson/icons.svg#bad", element("symbol", inside)))

    assert inspected_use_references(tree) == frozenset()


def test_an_unloaded_empty_or_oversized_copy_is_not_inspected() -> None:
    oversized = element("symbol", *(element("path") for _ in range(MAXIMUM_COPY_NODES)))

    assert inspected_use_references(document(use("/lesson/icons.svg#a", loaded=False))) == set()
    assert inspected_use_references(document(use("/lesson/icons.svg#a"))) == set()
    assert inspected_use_references(document(use("/lesson/icons.svg#a", oversized))) == set()


def test_one_unsafe_witness_refuses_its_reference() -> None:
    """Every element naming a reference must show an inert copy."""
    tree = document(
        use("/lesson/icons.svg#menu", icon()),
        use("/lesson/icons.svg#menu", loaded=False),
        use("/lesson/icons.svg#tick", icon()),
    )

    assert inspected_use_references(tree) == frozenset({f"{SPRITE}#tick"})


def test_an_element_naming_two_references_witnesses_neither() -> None:
    both = element("use", href="/lesson/icons.svg#menu", **{"xlink:href": "/lesson/icons.svg#x"})
    both["shadowRoots"] = [copy(icon())]

    assert inspected_use_references(document(both)) == frozenset()
    assert inspected_use_references(document(both, use("/lesson/icons.svg#menu", icon()))) == {
        f"{SPRITE}#menu"
    }


@pytest.mark.parametrize(
    "reference,url",
    [
        ("https://elsewhere.test/icons.svg#menu", PAGE),
        ("/lesson/icons.svg#menu", "http://site.test/lesson/1"),
        ("data:image/svg+xml,<svg/>#menu", PAGE),
        ("#menu", PAGE),
    ],
    ids=["cross_origin", "plain_http_page", "data_url", "fragment"],
)
def test_only_same_origin_https_external_references_qualify(reference: str, url: str) -> None:
    assert inspected_use_references(document(use(reference, icon()), url=url)) == frozenset()


def test_animation_anywhere_on_the_page_inspects_nothing() -> None:
    tree = document(
        use("/lesson/icons.svg#menu", icon()),
        element("svg", element("animateTransform", attributeName="transform")),
    )

    assert inspected_use_references(tree) == frozenset()


def test_author_shadow_trees_are_walked_and_frames_are_not() -> None:
    host = element("div")
    host["shadowRoots"] = [
        {"nodeType": 11, "shadowRootType": "open", "children": [use("/a.svg#x", icon())]}
    ]
    frame = element("iframe")
    frame["contentDocument"] = document(use("/b.svg#y", icon()))

    assert inspected_use_references(document(host, frame)) == frozenset(
        {"https://site.test/a.svg#x"}
    )
