"""Focused mapping boundaries for public application views."""

from typing import Any, cast

import pytest

from agent_core.application.public_services import _content_view


def test_content_view_rejects_an_unmapped_domain_part_explicitly() -> None:
    with pytest.raises(TypeError, match="unsupported session message content part"):
        _content_view([cast(Any, object())])
