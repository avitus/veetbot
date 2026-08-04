"""Official MCP Python SDK adapter.

No module outside ``agent_core.adapters.mcp`` imports an MCP SDK type.
"""

from __future__ import annotations

import hmac
import json
import shlex
import threading
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from types import TracebackType
from typing import Any, Self

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client import Transport
from mcp.client import stdio as mcp_stdio
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import Prompt, Resource

from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import MCPTransportError, MCPUnauthorizedError
from agent_core.domain.mcp import (
    MCPAuthScheme,
    MCPCallResult,
    MCPDiscovery,
    MCPRemotePrompt,
    MCPRemoteResource,
    MCPRemoteTool,
    MCPServerConfig,
    MCPTransport,
)

_STDIO_SPAWN_LOCK = threading.Lock()


def _exact_environment_stdio_client(parameters: StdioServerParameters) -> Transport:
    """Spawn with exactly the constructed environment despite SDK inheritance.

    MCP SDK 2 merges a fixed process-derived environment before applying
    ``StdioServerParameters.env``. Every platform MCP SDK import is confined to
    this package, so a process-wide lock can cover every stdio spawn while the
    adapter suppresses that SDK default for the spawn window.
    """

    @asynccontextmanager
    async def exact_transport() -> AsyncIterator[Any]:
        transport = mcp_stdio.stdio_client(parameters)
        await anyio.to_thread.run_sync(_STDIO_SPAWN_LOCK.acquire, abandon_on_cancel=False)
        try:
            inherited = mcp_stdio.DEFAULT_INHERITED_ENV_VARS
            mcp_stdio.DEFAULT_INHERITED_ENV_VARS = []
            try:
                streams = await transport.__aenter__()
            finally:
                mcp_stdio.DEFAULT_INHERITED_ENV_VARS = inherited
        finally:
            _STDIO_SPAWN_LOCK.release()
        try:
            yield streams
        finally:
            await transport.__aexit__(None, None, None)

    return exact_transport()


def _unauthorized(exc: BaseException) -> bool:
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "status_code", None) == 401:
            return True
        response = getattr(current, "response", None)
        if getattr(response, "status_code", None) == 401:
            return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )
    return False


def _text_content(value: object) -> tuple[str, ...]:
    content = getattr(value, "content", None)
    if content is None:
        content = getattr(value, "contents", ())
    if not isinstance(content, (list, tuple)):
        content = (content,)
    result: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            result.append(text)
            continue
        resource = getattr(item, "resource", None)
        resource_text = getattr(resource, "text", None)
        if isinstance(resource_text, str):
            result.append(resource_text)
            continue
        dumper = getattr(item, "model_dump_json", None)
        result.append(dumper() if callable(dumper) else str(item))
    return tuple(result)


class SDKMCPClient:
    def __init__(
        self,
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
        *,
        http_proxy_url: str | None = None,
    ) -> None:
        self._config = config
        self._credential = credential
        self._environment = dict(environment)
        self._http_proxy_url = http_proxy_url
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> Self:
        await self._connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._close(exc_type, exc, traceback)

    async def _headers(self) -> dict[str, str]:
        if self._config.auth_scheme is MCPAuthScheme.NONE:
            return {}
        if self._credential is None:
            raise MCPUnauthorizedError
        if self._config.auth_scheme is MCPAuthScheme.BEARER:
            return {"Authorization": f"Bearer {self._credential.reveal()}"}
        if self._config.auth_scheme is MCPAuthScheme.HEADER:
            if self._config.auth_name is None:
                raise MCPUnauthorizedError
            return {self._config.auth_name: self._credential.reveal()}
        if self._config.auth_scheme is MCPAuthScheme.OAUTH2_CLIENT:
            return {"Authorization": f"Bearer {await self._exchange_client_token()}"}
        return {}

    async def _exchange_client_token(self) -> str:
        if self._credential is None or self._config.token_endpoint is None:
            raise MCPUnauthorizedError
        try:
            parsed = json.loads(self._credential.reveal())
            if not isinstance(parsed, dict):
                raise ValueError
            client_id = parsed["client_id"]
            client_secret = parsed["client_secret"]
            if not isinstance(client_id, str) or not isinstance(client_secret, str):
                raise ValueError
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise MCPUnauthorizedError from exc
        try:
            async with httpx2.AsyncClient(proxy=self._http_proxy_url) as client:
                response = await client.post(
                    self._config.token_endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "scope": " ".join(self._config.token_scopes),
                    },
                    auth=(client_id, client_secret),
                )
                if response.status_code == 401:
                    raise MCPUnauthorizedError
                response.raise_for_status()
                payload = response.json()
        except MCPUnauthorizedError:
            raise
        except Exception as exc:
            raise MCPTransportError from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise MCPUnauthorizedError
        return token

    async def _connect(self) -> None:
        stack = AsyncExitStack()
        try:
            if self._config.transport is MCPTransport.STDIO:
                command = shlex.split(self._config.endpoint)
                if not command:
                    raise ValueError("MCP stdio command is empty")
                transport = _exact_environment_stdio_client(
                    StdioServerParameters(
                        command=command[0],
                        args=command[1:],
                        env=dict(self._environment),
                    )
                )
            else:
                http_client = await stack.enter_async_context(
                    httpx2.AsyncClient(
                        headers=await self._headers(),
                        proxy=self._http_proxy_url,
                    )
                )
                transport = streamable_http_client(
                    self._config.endpoint,
                    http_client=http_client,
                )
            client = Client(
                transport,
                read_timeout_seconds=float(self._config.timeout_seconds),
                sampling_callback=None,
                list_roots_callback=None,
                elicitation_callback=None,
            )
            self._client = await stack.enter_async_context(client)
            self._stack = stack.pop_all()
        except Exception as exc:
            await stack.aclose()
            if _unauthorized(exc):
                raise MCPUnauthorizedError from exc
            if isinstance(exc, (MCPUnauthorizedError, MCPTransportError)):
                raise
            raise MCPTransportError from exc

    async def _close(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        stack, self._stack = self._stack, None
        self._client = None
        if stack is not None:
            await stack.__aexit__(exc_type, exc, traceback)

    def _connected(self) -> Client:
        if self._client is None:
            raise MCPTransportError
        return self._client

    async def discover(self) -> MCPDiscovery:
        try:
            client = self._connected()
            listed_tools = await client.list_tools()
            capabilities = client.server_capabilities
            prompt_declarations: list[Prompt] = []
            if capabilities.prompts is not None:
                prompt_declarations = (await client.list_prompts()).prompts
            resource_declarations: list[Resource] = []
            if capabilities.resources is not None:
                resource_declarations = (await client.list_resources()).resources
            prompts: list[MCPRemotePrompt] = []
            for prompt in prompt_declarations:
                try:
                    rendered = await client.get_prompt(prompt.name, {})
                except MCPError as exc:
                    if _unauthorized(exc):
                        raise MCPUnauthorizedError from exc
                    continue
                body = "\n".join(
                    text for message in rendered.messages for text in _text_content(message)
                )
                if body:
                    prompts.append(
                        MCPRemotePrompt(
                            name=prompt.name,
                            description=prompt.description or "",
                            body=body,
                        )
                    )
            return MCPDiscovery(
                tools=tuple(
                    MCPRemoteTool(
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=dict(tool.input_schema),
                    )
                    for tool in listed_tools.tools
                ),
                prompts=tuple(prompts),
                resources=tuple(
                    MCPRemoteResource(
                        uri=str(resource.uri),
                        name=resource.name,
                        description=resource.description or "",
                    )
                    for resource in resource_declarations
                ),
            )
        except Exception as exc:
            if _unauthorized(exc):
                raise MCPUnauthorizedError from exc
            if isinstance(exc, (MCPUnauthorizedError, MCPTransportError)):
                raise
            raise MCPTransportError from exc

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
        try:
            result = await self._connected().call_tool(name, arguments)
            structured = getattr(result, "structured_content", None)
            return MCPCallResult(
                content=_text_content(result),
                structured=structured if isinstance(structured, dict) else None,
                is_error=bool(getattr(result, "is_error", False)),
            )
        except Exception as exc:
            if _unauthorized(exc):
                raise MCPUnauthorizedError from exc
            if isinstance(exc, MCPUnauthorizedError):
                raise
            raise MCPTransportError from exc

    async def read_resource(self, uri: str | None) -> MCPCallResult:
        try:
            if uri is None:
                resources = await self._connected().list_resources()
                return MCPCallResult(
                    content=tuple(str(resource.uri) for resource in resources.resources),
                    structured={
                        "resources": [str(resource.uri) for resource in resources.resources]
                    },
                )
            result = await self._connected().read_resource(uri)
            return MCPCallResult(content=_text_content(result))
        except Exception as exc:
            if _unauthorized(exc):
                raise MCPUnauthorizedError from exc
            if isinstance(exc, MCPUnauthorizedError):
                raise
            raise MCPTransportError from exc

    async def reauthenticate(
        self,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> bool:
        previous = None if self._credential is None else self._credential.reveal().encode()
        replacement = None if credential is None else credential.reveal().encode()
        if previous is None or replacement is None:
            changed = previous != replacement
        else:
            changed = not hmac.compare_digest(previous, replacement)
        if self._config.auth_scheme is MCPAuthScheme.OAUTH2_CLIENT and replacement is not None:
            changed = True
        if not changed:
            return False
        await self._close()
        self._credential = credential
        self._environment = dict(environment)
        await self._connect()
        return True


class SDKMCPClientFactory:
    def __init__(self, *, http_proxy_url: str | None = None) -> None:
        self._http_proxy_url = http_proxy_url

    def __call__(
        self,
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> SDKMCPClient:
        if config.transport is MCPTransport.HTTP and self._http_proxy_url is None:
            raise MCPTransportError
        return SDKMCPClient(
            config,
            credential,
            environment,
            http_proxy_url=self._http_proxy_url,
        )
