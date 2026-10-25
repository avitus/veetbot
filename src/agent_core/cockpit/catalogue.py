"""The kit catalogue `cockpit.read` serves as describe_kit (Milestone 35).

`cockpit/scripts/kit-catalogue.mjs` generates it from the kit's own source,
and the workspace's tests fail when the committed copy falls behind the kit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KIT_CATALOGUE_PATH = Path(__file__).with_name("kit-catalogue.json")


def load_kit_catalogue() -> dict[str, Any]:
    catalogue = json.loads(KIT_CATALOGUE_PATH.read_text(encoding="utf-8"))
    if not isinstance(catalogue, dict):
        raise ValueError("the kit catalogue is a JSON object")
    return catalogue
