"""Run-local profile and proxy router for internal Playwright MCP backend processes."""

import argparse
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
import hmac
import json
import os
import re
import shutil
import socket
from itertools import combinations
from pathlib import Path
from tempfile import mkstemp
from typing import Protocol, Self

from aiohttp import ClientConnectionResetError, ClientResponse, ClientSession, ClientTimeout, web
from multidict import CIMultiDict, MultiDict, MultiMapping
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from workflow_container_contract import network_proxy_name_validate

from browser_runtime.config import (
    DEFAULT_BROWSER_LOCALE,
    DEFAULT_BROWSER_TIMEZONE,
    BrowserLocaleConfig,
    NetworkProxyConfig,
)
from browser_runtime.playwright_mcp import (
    DEFAULT_ACTION_TIMEOUT_MS,
    DEFAULT_ALLOWED_HOST_LIST,
    DEFAULT_MCP_HOST,
    DEFAULT_PORT,
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    PlaywrightMcpBackend,
    PlaywrightMcpConfig,
    _allowed_host_list_with_port_get,
)
from browser_runtime.playwright_profile import playwright_profile_replace, playwright_profile_snapshot

_DEFAULT_CANDIDATE_ROOT_PATH = Path("/runtime/mcp_playwright_profile/writeback_candidate")
_DEFAULT_EXECUTION_STATE_RESTORE_MARKER_PATH = Path(
    "/runtime/mcp_playwright_profile/execution_state_restore.json"
)
_DEFAULT_MCP_BACKEND_RUNTIME_ROOT_PATH = Path("/runtime/playwright_mcp_backend")
_DEFAULT_MCP_OUTPUT_ROOT_PATH = Path("/output/.playwright-mcp")
_DEFAULT_PROFILE_ROOT_PATH = Path("/runtime/mcp_playwright_profile/profile")
_HOP_BY_HOP_HEADER_NAME_SET = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_PROFILE_NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,126}[A-Za-z0-9])?")
_ROUTER_QUERY_NAME_SET = {"network_proxy_name", "profile", "profile_source"}
_EXECUTION_STATE_RESTORE_PATH = "/runtime/execution-state-restore"
_EXECUTION_STATE_RESTORE_IDENTITY_HEADER = "X-Browser-Runtime-Execution-State-Restore-Identity"
_EXECUTION_STATE_RESTORE_TOKEN_HEADER = "X-Browser-Runtime-Execution-State-Restore-Token"
_WRITEBACK_CANDIDATE_PATH = "/runtime/mcp-playwright-profile/writeback-candidate"


class PlaywrightMcpBackendProtocol(Protocol):
    """Lifecycle surface required from one internal MCP backend."""

    @property
    def url(self) -> str:
        """Return the active backend URL."""

    async def start(self) -> None:
        """Start the backend when it is not active."""

    async def stop(self) -> None:
        """Stop the backend and wait for process exit."""


class PlaywrightMcpBackendIdentity(BaseModel):
    """Identify one independent browser backend and working profile pair."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    network_proxy_name: str | None
    physical_profile: str | None

    @field_validator("network_proxy_name")
    @classmethod
    def network_proxy_name_validate(cls, network_proxy_name: str | None) -> str | None:
        """Validate the exact optional public proxy name.

        Args:
            network_proxy_name: Candidate stable name or direct-egress marker.

        Returns:
            Validated unchanged name or `None`.
        """

        return None if network_proxy_name is None else network_proxy_name_validate(network_proxy_name)

    def relative_path_get(self) -> Path:
        """Return one transparent path unique to this backend identity.

        Returns:
            Relative path using the real profile and stable proxy name.
        """

        egress_path = Path("direct")
        if self.network_proxy_name is not None:
            owner_id, vpn_config_name = self.network_proxy_name.split("/", maxsplit=1)
            egress_path = Path("proxy") / owner_id / vpn_config_name
        if self.physical_profile is None:
            return Path("isolated") / egress_path
        return Path("named") / egress_path / self.physical_profile

    def sort_key_get(self) -> str:
        """Return a deterministic key for multi-identity lock ordering.

        Returns:
            POSIX relative identity path.
        """

        return self.relative_path_get().as_posix()


class PlaywrightMcpRoute(BaseModel):
    """Carry one exact public browser route and optional reset source."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    network_proxy_name: str | None
    physical_profile: str | None
    source_physical_profile: str | None

    def backend_identity_get(self) -> PlaywrightMcpBackendIdentity:
        """Return the exact target backend identity.

        Returns:
            Target profile and proxy pair.
        """

        return PlaywrightMcpBackendIdentity(
            network_proxy_name=self.network_proxy_name,
            physical_profile=self.physical_profile,
        )

    def source_backend_identity_get(self) -> PlaywrightMcpBackendIdentity | None:
        """Return the exact same-proxy source identity when configured.

        Returns:
            Source profile pair, or `None` when no reset source exists.
        """

        if self.source_physical_profile is None:
            return None
        return PlaywrightMcpBackendIdentity(
            network_proxy_name=self.network_proxy_name,
            physical_profile=self.source_physical_profile,
        )


class PlaywrightMcpRouterConfig(BaseModel):
    """Validated paths and backend template owned by the public router."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True, validate_default=True)

    backend_config: PlaywrightMcpConfig
    backend_runtime_root_path: Path = _DEFAULT_MCP_BACKEND_RUNTIME_ROOT_PATH
    candidate_root_path: Path = _DEFAULT_CANDIDATE_ROOT_PATH
    execution_state_restore_marker_path: Path = _DEFAULT_EXECUTION_STATE_RESTORE_MARKER_PATH
    execution_state_restore_token: SecretStr = Field(min_length=1)
    host: str = "0.0.0.0"
    network_proxy_config: NetworkProxyConfig
    output_root_path: Path = _DEFAULT_MCP_OUTPUT_ROOT_PATH
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    profile_root_path: Path = _DEFAULT_PROFILE_ROOT_PATH

    @model_validator(mode="after")
    def _path_separation_validate(self) -> Self:
        """Require disjoint backend-local owner roots.

        Returns:
            Validated router configuration.

        Raises:
            ValueError: If two mutable owner roots are identical.
        """

        root_path_list = [
            self.backend_runtime_root_path,
            self.candidate_root_path,
            self.output_root_path,
            self.profile_root_path,
        ]
        for first_path, second_path in combinations(root_path_list, 2):
            if (
                first_path == second_path
                or first_path.is_relative_to(second_path)
                or second_path.is_relative_to(first_path)
            ):
                raise ValueError("router runtime, candidate, output, and profile roots must be disjoint")
        if any(
            self.execution_state_restore_marker_path == root_path
            or self.execution_state_restore_marker_path.is_relative_to(root_path)
            for root_path in root_path_list
        ):
            raise ValueError("execution state restore marker must be outside mutable browser roots")
        return self


class McpPlaywrightProfileRouter:
    """Route public MCP traffic to run-local profile-specific backends."""

    def __init__(
        self,
        config: PlaywrightMcpRouterConfig,
        backend_factory: Callable[[PlaywrightMcpConfig], PlaywrightMcpBackendProtocol] = PlaywrightMcpBackend,
    ) -> None:
        """Store configuration and lazy backend factory.

        Args:
            config: Router filesystem and backend template configuration.
            backend_factory: Callable creating one backend lifecycle owner.
        """

        self.config = config
        self._backend_by_identity_map: dict[PlaywrightMcpBackendIdentity, PlaywrightMcpBackendProtocol] = {}
        self._backend_factory = backend_factory
        self._backend_port_set: set[int] = set()
        self._candidate_lock = asyncio.Lock()
        self._client_session: ClientSession | None = None
        self._execution_proxy_task_set: set[asyncio.Task[object]] = set()
        self._execution_state_condition = asyncio.Condition()
        self._execution_state_restore_in_progress = False
        self._leased_request_count = 0
        self._lock_by_identity_map: dict[PlaywrightMcpBackendIdentity, asyncio.Lock] = {}

    async def close(self) -> None:
        """Close all active internal backends and the proxy client session."""

        async with self._execution_state_restore_scope():
            await self._backend_list_stop()
            self._backend_state_forget()
        if self._client_session is not None:
            await self._client_session.close()
            self._client_session = None

    async def request_proxy(self, request: web.Request) -> web.StreamResponse:
        """Proxy one MCP request through the backend selected by the route query."""

        async with self._request_lease(is_interruptible=True):
            return await self._request_proxy(request)

    async def _request_proxy(self, request: web.Request) -> web.StreamResponse:
        """Proxy one MCP request while holding an execution-state lease."""

        try:
            self._host_validate(request)
            route = self._route_from_request(request)
            backend_identity = route.backend_identity_get()
            request_body = await request.read()
            source_reset_need = self._source_reset_need(request=request, request_body=request_body, route=route)
            source_backend_identity = route.source_backend_identity_get()
            if (
                not source_reset_need
                and source_backend_identity is not None
                and not self._profile_path_get(source_backend_identity).is_dir()
            ):
                raise FileNotFoundError(f"source profile is missing: {route.source_physical_profile}")
            lock_identity_list = [backend_identity]
            if source_reset_need:
                if source_backend_identity is None:
                    raise RuntimeError("source reset requires source_physical_profile")
                lock_identity_list.append(source_backend_identity)
            async with AsyncExitStack() as stack:
                for lock_identity in sorted(
                    lock_identity_list,
                    key=PlaywrightMcpBackendIdentity.sort_key_get,
                ):
                    await stack.enter_async_context(self._identity_lock_get(lock_identity))
                if source_reset_need:
                    await self._profile_source_apply(route)
                elif route.physical_profile is not None and not self._profile_path_get(backend_identity).exists():
                    await asyncio.to_thread(
                        playwright_profile_replace,
                        source_profile_path=self.config.backend_config.secret_root_path / "playwright_profile",
                        target_profile_path=self._profile_path_get(backend_identity),
                    )
                backend = await self._backend_get_start(backend_identity)
                upstream_response = await self._backend_request_get(
                    backend=backend,
                    request=request,
                    request_body=request_body,
                )
                if source_reset_need:
                    return await self._backend_response_proxy(request=request, upstream_response=upstream_response)
            return await self._backend_response_proxy(request=request, upstream_response=upstream_response)
        except (FileNotFoundError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    async def writeback_candidate_publish(self, request: web.Request) -> web.Response:
        """Stop one named backend and atomically publish its current profile."""

        async with self._request_lease(is_interruptible=False):
            return await self._writeback_candidate_publish(request)

    async def _writeback_candidate_publish(self, request: web.Request) -> web.Response:
        """Publish one candidate while holding an execution-state lease."""

        try:
            self._host_validate(request)
            if await request.read():
                raise ValueError("writeback candidate request body must be empty")
            backend_identity = self._candidate_backend_identity_from_request(request)
            async with self._identity_lock_get(backend_identity):
                async with self._candidate_lock:
                    await self._profile_candidate_publish(backend_identity)
            return web.Response(status=204)
        except (FileNotFoundError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    async def execution_state_restore(self, request: web.Request) -> web.Response:
        """Discard all attempt-local browser state after predecessor stop proof."""

        try:
            self._host_validate(request)
            token_list = request.headers.getall(_EXECUTION_STATE_RESTORE_TOKEN_HEADER, [])
            if len(token_list) != 1 or not hmac.compare_digest(
                token_list[0],
                self.config.execution_state_restore_token.get_secret_value(),
            ):
                raise web.HTTPForbidden(text="execution state restore credential is invalid")
            restore_identity_list = request.headers.getall(_EXECUTION_STATE_RESTORE_IDENTITY_HEADER, [])
            if len(restore_identity_list) != 1:
                raise ValueError("execution state restore requires one exact identity")
            restore_identity = restore_identity_list[0]
            if (
                not restore_identity
                or len(restore_identity) > 255
                or not restore_identity.isascii()
                or any(character.isspace() or not character.isprintable() for character in restore_identity)
            ):
                raise ValueError("execution state restore identity must be one printable ASCII token")
            if request.query:
                raise ValueError("execution state restore does not accept query parameters")
            if await request.read():
                raise ValueError("execution state restore request body must be empty")
            async with self._execution_state_restore_scope():
                if self._execution_state_restore_is_completed(restore_identity=restore_identity):
                    return web.Response(status=204)
                await self._backend_list_stop()
                await asyncio.to_thread(self._mutable_state_reset)
                self._backend_state_forget()
                await asyncio.to_thread(
                    self._execution_state_restore_mark_completed,
                    restore_identity=restore_identity,
                )
            return web.Response(status=204)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    @asynccontextmanager
    async def _request_lease(self, *, is_interruptible: bool) -> AsyncIterator[None]:
        """Keep mutable browser state stable for one complete router request."""

        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("browser request lease requires one current asyncio task")
        async with self._execution_state_condition:
            await self._execution_state_condition.wait_for(lambda: not self._execution_state_restore_in_progress)
            self._leased_request_count += 1
            if is_interruptible:
                self._execution_proxy_task_set.add(current_task)
        try:
            yield
        finally:
            async with self._execution_state_condition:
                self._execution_proxy_task_set.discard(current_task)
                self._leased_request_count -= 1
                self._execution_state_condition.notify_all()

    @asynccontextmanager
    async def _execution_state_restore_scope(self) -> AsyncIterator[None]:
        """Exclude router requests while all mutable browser state changes."""

        async with self._execution_state_condition:
            await self._execution_state_condition.wait_for(lambda: not self._execution_state_restore_in_progress)
            self._execution_state_restore_in_progress = True
            execution_proxy_task_list = list(self._execution_proxy_task_set)
        for execution_proxy_task in execution_proxy_task_list:
            execution_proxy_task.cancel()
        if execution_proxy_task_list:
            await asyncio.gather(*execution_proxy_task_list, return_exceptions=True)
        async with self._execution_state_condition:
            await self._execution_state_condition.wait_for(lambda: self._leased_request_count == 0)
        try:
            yield
        finally:
            async with self._execution_state_condition:
                self._execution_state_restore_in_progress = False
                self._execution_state_condition.notify_all()

    def _execution_state_restore_is_completed(self, *, restore_identity: str) -> bool:
        """Return whether the exact authenticated restore already completed."""

        marker_path = self.config.execution_state_restore_marker_path
        if marker_path.is_symlink() or not marker_path.is_file():
            return False
        try:
            marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(marker_payload, dict) or set(marker_payload) != {"restore_identity", "signature"}:
            return False
        marker_identity = marker_payload.get("restore_identity")
        marker_signature = marker_payload.get("signature")
        if marker_identity != restore_identity or not isinstance(marker_signature, str):
            return False
        return hmac.compare_digest(
            marker_signature,
            self._execution_state_restore_signature_get(restore_identity=restore_identity),
        )

    def _execution_state_restore_mark_completed(self, *, restore_identity: str) -> None:
        """Atomically persist one authenticated completed-restore identity."""

        marker_path = self.config.execution_state_restore_marker_path
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_payload = {
            "restore_identity": restore_identity,
            "signature": self._execution_state_restore_signature_get(restore_identity=restore_identity),
        }
        file_descriptor, temporary_path_raw = mkstemp(
            dir=marker_path.parent,
            prefix=f".{marker_path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_path_raw)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(marker_payload, temporary_file, sort_keys=True)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, marker_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _execution_state_restore_signature_get(self, *, restore_identity: str) -> str:
        """Return one credential-bound signature for a completed restore identity."""

        return hmac.digest(
            self.config.execution_state_restore_token.get_secret_value().encode(),
            restore_identity.encode(),
            "sha256",
        ).hex()

    async def _backend_list_stop(self) -> None:
        """Stop every current backend before shared state changes."""

        await asyncio.gather(*(backend.stop() for backend in self._backend_by_identity_map.values()))

    def _backend_state_forget(self) -> None:
        """Forget stopped backend identities, ports, and pair locks."""

        self._backend_by_identity_map.clear()
        self._backend_port_set.clear()
        self._lock_by_identity_map.clear()

    def _mutable_state_reset(self) -> None:
        """Clear attempt-local roots without replacing their mounted directories."""

        for root_path in [
            self.config.backend_runtime_root_path,
            self.config.candidate_root_path,
            self.config.output_root_path,
            self.config.profile_root_path,
        ]:
            if root_path.is_symlink():
                raise ValueError("browser mutable state root must not be a symbolic link")
            if root_path.exists() and not root_path.is_dir():
                raise ValueError("browser mutable state root must be one directory")
            root_path.mkdir(parents=True, exist_ok=True)
            for child_path in root_path.iterdir():
                if child_path.is_symlink() or child_path.is_file():
                    child_path.unlink()
                elif child_path.is_dir():
                    shutil.rmtree(child_path)
                else:
                    raise ValueError("browser mutable state contains an unsupported filesystem entry")

    async def _backend_request_get(
        self,
        backend: PlaywrightMcpBackendProtocol,
        request: web.Request,
        request_body: bytes,
    ) -> ClientResponse:
        """Deliver one request and return the upstream response after headers arrive."""

        client_session = self._client_session_get()
        query = MultiDict((name, value) for name, value in request.query.items() if name not in _ROUTER_QUERY_NAME_SET)
        request_headers = self._end_to_end_headers_get(request.headers)
        for name in ["content-length", "host"]:
            request_headers.popall(name, None)
        return await client_session.request(
            method=request.method,
            url=f"{backend.url}{request.rel_url.raw_path}",
            params=query,
            headers=request_headers,
            data=request_body,
            allow_redirects=False,
        )

    async def _backend_response_proxy(
        self,
        request: web.Request,
        upstream_response: ClientResponse,
    ) -> web.StreamResponse:
        """Stream one delivered upstream response to the public MCP client."""

        response_headers = self._end_to_end_headers_get(upstream_response.headers)
        response = web.StreamResponse(
            status=upstream_response.status, reason=upstream_response.reason, headers=response_headers
        )
        try:
            await response.prepare(request)
            async for chunk in upstream_response.content.iter_chunked(64 * 1024):
                try:
                    await response.write(chunk)
                except ClientConnectionResetError:
                    return response
            try:
                await response.write_eof()
            except ClientConnectionResetError:
                return response
            return response
        finally:
            upstream_response.release()

    async def _backend_get_start(
        self,
        backend_identity: PlaywrightMcpBackendIdentity,
    ) -> PlaywrightMcpBackendProtocol:
        """Create one backend lifecycle owner when absent and ensure it is running."""

        backend = self._backend_by_identity_map.get(backend_identity)
        if backend is None:
            backend = self._backend_factory(self._backend_config_get(backend_identity))
            self._backend_by_identity_map[backend_identity] = backend
        await backend.start()
        return backend

    def _backend_config_get(self, backend_identity: PlaywrightMcpBackendIdentity) -> PlaywrightMcpConfig:
        """Build one backend-local config with disjoint paths and an internal port."""

        backend_relative_path = backend_identity.relative_path_get()
        backend_runtime_path = self.config.backend_runtime_root_path / backend_relative_path
        persistent_profile_path = (
            None if backend_identity.physical_profile is None else self._profile_path_get(backend_identity)
        )
        port = _loopback_port_get()
        while port in self._backend_port_set:
            port = _loopback_port_get()
        config_payload = self.config.backend_config.model_dump(mode="python")
        config_payload.update(
            {
                "allowed_host_list": list(
                    dict.fromkeys([*self.config.backend_config.allowed_host_list, *DEFAULT_ALLOWED_HOST_LIST])
                ),
                "host": DEFAULT_MCP_HOST,
                "isolated": backend_identity.physical_profile is None,
                "mcp_config_path": backend_runtime_path / "config.json",
                "network_proxy_url": self.config.network_proxy_config.proxy_url_get(
                    backend_identity.network_proxy_name
                ),
                "output_dir": self.config.output_root_path / backend_relative_path,
                "persistent_profile_path": persistent_profile_path,
                "port": port,
            }
        )
        backend_config = PlaywrightMcpConfig(**config_payload)
        self._backend_port_set.add(backend_config.port)
        return backend_config

    def _client_session_get(self) -> ClientSession:
        """Return the router-owned no-decompression proxy session."""

        if self._client_session is None:
            self._client_session = ClientSession(auto_decompress=False, timeout=ClientTimeout(total=None))
        return self._client_session

    @staticmethod
    def _end_to_end_headers_get(headers: MultiMapping[str]) -> CIMultiDict[str]:
        """Copy headers without fixed or Connection-declared hop-by-hop fields."""

        end_to_end_headers = CIMultiDict(headers)
        connection_header_list = end_to_end_headers.getall("connection", [])
        connection_option_set = {
            option.strip().lower()
            for connection_header in connection_header_list
            for option in connection_header.split(",")
            if option.strip()
        }
        for name in _HOP_BY_HOP_HEADER_NAME_SET | connection_option_set:
            end_to_end_headers.popall(name, None)
        return end_to_end_headers

    async def _profile_candidate_publish(self, backend_identity: PlaywrightMcpBackendIdentity) -> None:
        """Stop one backend and atomically snapshot its current profile."""

        if backend_identity.physical_profile is None:
            raise ValueError("writeback candidate requires a named profile")
        backend = self._backend_by_identity_map.get(backend_identity)
        if backend is not None:
            await backend.stop()
        await asyncio.to_thread(
            playwright_profile_snapshot,
            runtime_profile_path=self._profile_path_get(backend_identity),
            writeback_candidate_path=self.config.candidate_root_path,
        )
        print(
            json.dumps(
                {
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "event_name": "browser_runtime.playwright_mcp_router.writeback_candidate_publication_completed",
                    "network_proxy_name": backend_identity.network_proxy_name,
                    "physical_profile": backend_identity.physical_profile,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def _identity_lock_get(self, backend_identity: PlaywrightMcpBackendIdentity) -> asyncio.Lock:
        """Return the stable lock for one exact profile and proxy identity."""

        lock = self._lock_by_identity_map.get(backend_identity)
        if lock is None:
            lock = asyncio.Lock()
            self._lock_by_identity_map[backend_identity] = lock
        return lock

    def _profile_path_get(self, backend_identity: PlaywrightMcpBackendIdentity) -> Path:
        """Return the pair-local named-profile working directory.

        Args:
            backend_identity: Exact profile and proxy identity.

        Returns:
            Transparent run-local working profile path.

        Raises:
            ValueError: If the identity describes an isolated backend.
        """

        if backend_identity.physical_profile is None:
            raise ValueError("isolated backend has no persistent profile path")
        return self.config.profile_root_path / backend_identity.relative_path_get()

    async def _profile_source_apply(self, route: PlaywrightMcpRoute) -> None:
        """Stop the target backend and atomically reset it from the explicit source."""

        if route.physical_profile is None or route.source_physical_profile is None:
            raise RuntimeError("profile source reset requires named source and target")
        backend_identity = route.backend_identity_get()
        source_backend_identity = route.source_backend_identity_get()
        if source_backend_identity is None:
            raise RuntimeError("profile source reset requires a source identity")
        source_profile_path = self._profile_path_get(source_backend_identity)
        if not source_profile_path.is_dir():
            raise FileNotFoundError(f"source profile is missing: {route.source_physical_profile}")
        backend = self._backend_by_identity_map.get(backend_identity)
        if backend is not None:
            await backend.stop()
        await asyncio.to_thread(
            playwright_profile_replace,
            source_profile_path=source_profile_path,
            target_profile_path=self._profile_path_get(backend_identity),
        )

    def _candidate_backend_identity_from_request(self, request: web.Request) -> PlaywrightMcpBackendIdentity:
        """Parse the exact writeback candidate profile and proxy identity."""

        unknown_name_set = set(request.query) - {"network_proxy_name", "profile"}
        if unknown_name_set:
            raise ValueError(f"unexpected query parameter: {sorted(unknown_name_set)[0]}")
        route = self._route_from_request(request)
        if route.physical_profile is None:
            raise ValueError("profile is required")
        return route.backend_identity_get()

    def _host_validate(self, request: web.Request) -> None:
        """Reject a public Host header outside the configured MCP allow-list."""

        allowed_host_list = self.config.backend_config.allowed_host_list
        if "*" in allowed_host_list:
            return
        allowed_host_set = {
            allowed_host.lower()
            for allowed_host in _allowed_host_list_with_port_get(
                allowed_host_list=allowed_host_list,
                port=self.config.port,
            )
        }
        if request.headers.get("host", "").lower() not in allowed_host_set:
            raise ValueError("request Host is not allowed")

    def _route_from_request(self, request: web.Request) -> PlaywrightMcpRoute:
        """Parse one public route without losing duplicate query values."""

        network_proxy_name = self._query_value_get(request=request, name="network_proxy_name")
        physical_profile = self._query_value_get(request=request, name="profile")
        source_physical_profile = self._query_value_get(request=request, name="profile_source")
        self.config.network_proxy_config.proxy_url_get(network_proxy_name)
        if physical_profile is None and source_physical_profile is not None:
            raise ValueError("profile_source requires profile")
        if physical_profile is not None:
            physical_profile = self._profile_name_validate(name="profile", value=physical_profile)
        if source_physical_profile is not None:
            source_physical_profile = self._profile_name_validate(name="profile_source", value=source_physical_profile)
        if physical_profile is not None and physical_profile == source_physical_profile:
            raise ValueError("profile_source must differ from profile")
        return PlaywrightMcpRoute(
            network_proxy_name=network_proxy_name,
            physical_profile=physical_profile,
            source_physical_profile=source_physical_profile,
        )

    @staticmethod
    def _profile_name_validate(name: str, value: str) -> str:
        """Reject a physical profile name that is unsafe for one path segment."""

        if _PROFILE_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{name} is unsafe")
        return value

    @staticmethod
    def _query_value_get(request: web.Request, name: str) -> str | None:
        """Return one optional query value while rejecting duplicates."""

        value_list = request.query.getall(name, [])
        if len(value_list) > 1:
            raise ValueError(f"{name} must occur at most once")
        return value_list[0] if value_list else None

    @staticmethod
    def _source_reset_need(request: web.Request, request_body: bytes, route: PlaywrightMcpRoute) -> bool:
        """Return whether this request starts a new explicit-source MCP session."""

        if route.source_physical_profile is None or request.method != "POST" or "mcp-session-id" in request.headers:
            return False
        try:
            request_payload = json.loads(request_body)
        except json.JSONDecodeError, UnicodeDecodeError:
            return False
        return isinstance(request_payload, dict) and request_payload.get("method") == "initialize"


def _args_parse() -> argparse.Namespace:
    """Parse the public Playwright MCP profile router CLI."""

    parser = argparse.ArgumentParser(description="Route Playwright MCP requests by run-local physical profile.")
    parser.add_argument("--action-timeout-ms", default=DEFAULT_ACTION_TIMEOUT_MS, type=int)
    parser.add_argument(
        "--allowed-hosts", default=DEFAULT_ALLOWED_HOST_LIST, dest="allowed_host_list", type=_allowed_host_list_parse
    )
    parser.add_argument("--backend-runtime-root-path", default=_DEFAULT_MCP_BACKEND_RUNTIME_ROOT_PATH, type=Path)
    parser.add_argument("--candidate-root-path", default=_DEFAULT_CANDIDATE_ROOT_PATH, type=Path)
    parser.add_argument(
        "--execution-state-restore-marker-path",
        default=_DEFAULT_EXECUTION_STATE_RESTORE_MARKER_PATH,
        type=Path,
    )
    parser.add_argument("--secret-root-path", required=True, type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--locale", default=DEFAULT_BROWSER_LOCALE)
    parser.add_argument("--navigation-timeout-ms", default=60000, type=int)
    parser.add_argument("--network-proxy-config-path", required=True, type=Path)
    parser.add_argument("--output-root-path", default=_DEFAULT_MCP_OUTPUT_ROOT_PATH, type=Path)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--profile-root-path", default=_DEFAULT_PROFILE_ROOT_PATH, type=Path)
    parser.add_argument("--readiness-timeout-seconds", default=DEFAULT_READINESS_TIMEOUT_SECONDS, type=int)
    parser.add_argument("--timezone", default=DEFAULT_BROWSER_TIMEZONE)
    parser.add_argument("--viewport-height", default=1080, type=int)
    parser.add_argument("--viewport-width", default=1920, type=int)
    return parser.parse_args()


def _allowed_host_list_parse(value: str) -> list[str]:
    """Parse a non-empty comma-separated backend allowed-host list."""

    allowed_host_list = [host.strip() for host in value.split(",") if host.strip()]
    if not allowed_host_list:
        raise argparse.ArgumentTypeError("allowed-hosts must contain at least one host")
    return allowed_host_list


def _loopback_port_get() -> int:
    """Reserve and release one currently available loopback TCP port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((DEFAULT_MCP_HOST, 0))
        return int(server_socket.getsockname()[1])


def main() -> None:
    """Run the public aiohttp profile router until the process is stopped."""

    argument_by_name_map = vars(_args_parse())
    host = argument_by_name_map.pop("host")
    port = argument_by_name_map.pop("port")
    locale = argument_by_name_map.pop("locale")
    network_proxy_config_path = argument_by_name_map.pop("network_proxy_config_path")
    backend_runtime_root_path = argument_by_name_map.pop("backend_runtime_root_path")
    candidate_root_path = argument_by_name_map.pop("candidate_root_path")
    execution_state_restore_marker_path = argument_by_name_map.pop("execution_state_restore_marker_path")
    output_root_path = argument_by_name_map.pop("output_root_path")
    profile_root_path = argument_by_name_map.pop("profile_root_path")
    backend_config = PlaywrightMcpConfig(
        host=DEFAULT_MCP_HOST,
        locale_config=BrowserLocaleConfig(locale=locale),
        mcp_config_path=backend_runtime_root_path / "base" / "config.json",
        output_dir=output_root_path / "base",
        persistent_profile_path=profile_root_path / "_template",
        port=DEFAULT_PORT,
        **argument_by_name_map,
    )
    router_config = PlaywrightMcpRouterConfig(
        backend_config=backend_config,
        backend_runtime_root_path=backend_runtime_root_path,
        candidate_root_path=candidate_root_path,
        execution_state_restore_marker_path=execution_state_restore_marker_path,
        execution_state_restore_token=SecretStr(os.environ["BrowserRuntimeExecutionStateRestoreToken"]),
        host=host,
        network_proxy_config=NetworkProxyConfig.from_path(network_proxy_config_path),
        output_root_path=output_root_path,
        port=port,
        profile_root_path=profile_root_path,
    )
    router = McpPlaywrightProfileRouter(config=router_config)
    application = web.Application()
    application.router.add_post(_EXECUTION_STATE_RESTORE_PATH, router.execution_state_restore)
    application.router.add_post(_WRITEBACK_CANDIDATE_PATH, router.writeback_candidate_publish)
    application.router.add_route("*", "/{path:.*}", router.request_proxy)
    application.on_cleanup.append(lambda application: router.close())
    web.run_app(application, host=router_config.host, port=router_config.port)


if __name__ == "__main__":
    main()
