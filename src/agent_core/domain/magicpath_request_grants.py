"""The bounded MagicPath design offer carried by a tool-name approval card."""

from datetime import datetime
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

MagicPathToolName = Literal[
    "mcp.magicpath.get_active_projects",
    "mcp.magicpath.list_projects",
    "mcp.magicpath.get_canvas_context",
    "mcp.magicpath.create_canvas_shapes",
]
MAGICPATH_TOOLS: Final = frozenset(get_args(MagicPathToolName))
PROJECT_LOOKUPS: Final = frozenset(
    {"mcp.magicpath.get_active_projects", "mcp.magicpath.list_projects"}
)
MAGICPATH_ENDPOINT: Final = "https://api.magicpath.ai/mcp"
MAGICPATH_GRANT_CREATED: Final = "magicpath.request_grant.created"


class MagicPathRequestGrantOffer(BaseModel):
    """The pinned catalog, login and optional project already named on the card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: MagicPathToolName
    duration_seconds: Literal[7200] = 7200
    summary: str = Field(min_length=1, max_length=1024)
    catalog_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    signed_in_at: datetime
    project_id: str | None = Field(default=None, min_length=1, max_length=128)
