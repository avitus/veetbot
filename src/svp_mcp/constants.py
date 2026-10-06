"""Fixed endpoints, limits and failure codes; nothing here is configurable."""

from __future__ import annotations

from typing import Final

ORIGIN: Final = "https://scalevp-mcp.com"
# The published document sits at the API mount and spells its paths relative to
# it (`/v1/...`); only URLs under the version root are ever requested.
API_BASE: Final = ORIGIN + "/api"
API_ROOT: Final = API_BASE + "/v1/"
DOCUMENT_URL: Final = API_BASE + "/openapi.json"
# The only `POST` operations offered: reviewed searches and lookups that read
# and change nothing. Every other non-`GET` operation stays invisible.
READ_POST_PATHS: Final = frozenset(
    {
        "/v1/companies/_lookup",
        "/v1/companies/_search",
        "/v1/contacts/_search",
        "/v1/deals/_search",
        "/v1/investors/_coinvestors",
        "/v1/investors/_portfolio",
        "/v1/search",
    }
)
AUTHORIZATION_ENDPOINT: Final = ORIGIN + "/authorize"
TOKEN_ENDPOINT: Final = ORIGIN + "/token"
REGISTRATION_ENDPOINT: Final = ORIGIN + "/register"
RESOURCE: Final = ORIGIN + "/"
SCOPES: Final = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)
CLIENT_NAME: Final = "Veetbot"
LOOPBACK_REDIRECT_HOST: Final = "127.0.0.1"
LOOPBACK_REDIRECT_PORT: Final = 8791
LOOPBACK_REDIRECT_URI: Final = f"http://{LOOPBACK_REDIRECT_HOST}:{LOOPBACK_REDIRECT_PORT}/callback"
CREDENTIAL_VARIABLE: Final = "SVP_MCP_CREDENTIAL_FILE"

EXPIRY_SKEW_SECONDS: Final = 60
DEFAULT_EXPIRY_SECONDS: Final = 300
MAXIMUM_TOKEN_BYTES: Final = 65_536
MAXIMUM_STATE_BYTES: Final = 65_536
MAXIMUM_RESULT_BYTES: Final = 524_288
MAXIMUM_BODY_BYTES: Final = 16_384
# Client errors the service explains in a message a caller can act on.
ANSWERED_STATUSES: Final = frozenset({400, 404, 409, 422})
MAXIMUM_PROBLEM_BYTES: Final = 16_384
MAXIMUM_PROBLEM_CHARACTERS: Final = 2000
MAXIMUM_DOCUMENT_BYTES: Final = 4_194_304
MAXIMUM_DESCRIPTION_BYTES: Final = 65_536
MAXIMUM_LISTED_OPERATIONS: Final = 500

STABLE_FAILURE_CODES: Final = frozenset(
    {
        "svp.arguments_invalid",
        "svp.credential_invalid",
        "svp.credential_rejected",
        "svp.operation_unknown",
        "svp.provider_rejected",
        "svp.provider_unavailable",
        "svp.rate_limited",
        "svp.response_invalid",
        "svp.response_too_large",
        "svp.specification_invalid",
    }
)
