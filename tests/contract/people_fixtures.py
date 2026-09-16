"""Typed shared owner and timestamp fields for People contract fixtures."""

from datetime import datetime
from typing import TypedDict


class PeopleFields(TypedDict):
    tenant_id: str
    principal_id: str
    created_at: datetime
    updated_at: datetime
