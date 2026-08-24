"""Shared route inspection helpers for the Milestone 17 memory API tests."""

from typing import Any

from fastapi.routing import APIRoute


def memory_routes(app: Any) -> list[APIRoute]:
    """Return every mounted `/v1/memories` route, including nested routers."""

    flattened = [
        nested
        for route in app.routes
        for nested in (
            route.original_router.routes if hasattr(route, "original_router") else (route,)
        )
    ]
    return [
        route
        for route in flattened
        if isinstance(route, APIRoute) and route.path.startswith("/v1/memories")
    ]
