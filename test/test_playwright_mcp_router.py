"""Behavior tests for the run-local Playwright MCP profile router."""

import asyncio
import builtins
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from aiohttp import ClientConnectionResetError, ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr, ValidationError

from browser_runtime.config import NetworkProxyConfig
from browser_runtime.playwright_mcp import PlaywrightMcpConfig
from browser_runtime.playwright_mcp_router import (
    _DEFAULT_CANDIDATE_ROOT_PATH,
    _DEFAULT_EXECUTION_STATE_RESTORE_MARKER_PATH,
    _EXECUTION_STATE_RESTORE_IDENTITY_HEADER,
    _EXECUTION_STATE_RESTORE_TOKEN_HEADER,
    McpPlaywrightProfileRouter,
    PlaywrightMcpBackendIdentity,
    PlaywrightMcpRouterConfig,
    _args_parse,
)


class FakePlaywrightMcpBackend:
    """In-process upstream that records backend lifecycle and generated config."""

    def __init__(self, config: PlaywrightMcpConfig, event_list: list[str]) -> None:
        """Store one backend config and shared lifecycle event list."""

        self.config = config
        self.event_list = event_list
        self.start_count = 0
        self.stop_count = 0
        self.hold_response_body_release: asyncio.Event | None = None
        self.hold_response_headers_sent: asyncio.Event | None = None
        self.stop_request_started = asyncio.Event()
        self._server: TestServer | None = None

    @property
    def url(self) -> str:
        """Return the active fake upstream URL."""

        if self._server is None:
            raise RuntimeError("backend is not running")
        return str(self._server.make_url(""))

    async def start(self) -> None:
        """Start one fake backend server."""

        if self._server is not None:
            return
        application = web.Application()
        application.router.add_route("*", "/{path:.*}", self._request_handle)
        self._server = TestServer(application)
        await self._server.start_server()
        self.start_count += 1
        self.event_list.append(f"start:{self._profile_name_get()}")

    async def stop(self) -> None:
        """Stop the fake backend server."""

        if self._server is None:
            return
        self.stop_request_started.set()
        await self._server.close()
        self._server = None
        self.stop_count += 1
        self.event_list.append(f"stop:{self._profile_name_get()}")

    def _profile_name_get(self) -> str:
        """Return the physical profile name represented by this backend."""

        return "isolated" if self.config.persistent_profile_path is None else self.config.persistent_profile_path.name

    async def _request_handle(self, request: web.Request) -> web.StreamResponse:
        """Return backend identity or one streaming MCP response."""

        if request.path == "/stream":
            response = web.StreamResponse(
                status=207,
                headers={"content-type": "text/event-stream", "mcp-session-id": "session-7", "x-upstream": "yes"},
            )
            await response.prepare(request)
            await response.write(b"event: message\n")
            await response.write(b"data: one\n\n")
            await response.write_eof()
            return response
        if request.path == "/hold":
            if self.hold_response_body_release is None or self.hold_response_headers_sent is None:
                raise RuntimeError("hold request events are not configured")
            response = web.StreamResponse(status=200, headers={"content-type": "application/json"})
            await response.prepare(request)
            self.hold_response_headers_sent.set()
            await self.hold_response_body_release.wait()
            await response.write(b'{"held": true}')
            await response.write_eof()
            return response
        return web.json_response(
            {
                "body": (await request.read()).decode(),
                "method": request.method,
                "profile": self._profile_name_get(),
                "query": list(request.query.items()),
            }
        )


class RouterFixture:
    """Own one router, fake backend set, and aiohttp client."""

    def __init__(self, tmp_path: Path, allowed_host_list: list[str] | None = None) -> None:
        """Create isolated run-local and immutable profile roots."""

        self.backend_list: list[FakePlaywrightMcpBackend] = []
        self.backend_by_profile_map: dict[str, FakePlaywrightMcpBackend] = {}
        self.event_list: list[str] = []
        self.hold_response_body_release = asyncio.Event()
        self.hold_response_headers_sent = asyncio.Event()
        self.secret_root_path = tmp_path / "secret-root"
        (self.secret_root_path / "playwright_profile").mkdir(parents=True)
        self.profile_root_path = tmp_path / "runtime" / "mcp_playwright_profile" / "profile"
        self.candidate_root_path = tmp_path / "candidate"
        self.execution_state_restore_marker_path = (
            tmp_path / "runtime" / "mcp_playwright_profile" / "execution_state_restore.json"
        )
        self.backend_runtime_root_path = tmp_path / "runtime" / "playwright_mcp_backend"
        self.output_root_path = tmp_path / "output" / ".playwright-mcp"
        backend_config = PlaywrightMcpConfig(
            allowed_host_list=["*"] if allowed_host_list is None else allowed_host_list,
            secret_root_path=self.secret_root_path,
            mcp_config_path=tmp_path / "runtime" / "playwright_mcp" / "base.json",
            output_dir=tmp_path / "output" / ".playwright-mcp",
            persistent_profile_path=tmp_path / "unused-profile",
        )

        def backend_factory(config: PlaywrightMcpConfig) -> FakePlaywrightMcpBackend:
            """Create and index one fake backend by physical profile name."""

            profile_name = "isolated" if config.persistent_profile_path is None else config.persistent_profile_path.name
            backend = FakePlaywrightMcpBackend(config, self.event_list)
            backend.hold_response_body_release = self.hold_response_body_release
            backend.hold_response_headers_sent = self.hold_response_headers_sent
            self.backend_list.append(backend)
            self.backend_by_profile_map[profile_name] = backend
            return backend

        self.router = McpPlaywrightProfileRouter(
            config=PlaywrightMcpRouterConfig(
                backend_config=backend_config,
                backend_runtime_root_path=self.backend_runtime_root_path,
                candidate_root_path=self.candidate_root_path,
                execution_state_restore_marker_path=self.execution_state_restore_marker_path,
                execution_state_restore_token=SecretStr("execution-state-restore-token"),
                network_proxy_config=NetworkProxyConfig(
                    proxy_by_name_map={
                        "user-a/proxy_a": "socks5://proxy-a:1080",
                        "user-b/proxy_b": "socks5://proxy-b:1080",
                    }
                ),
                output_root_path=self.output_root_path,
                profile_root_path=self.profile_root_path,
            ),
            backend_factory=backend_factory,
        )
        application = web.Application()
        application.router.add_post(
            "/runtime/execution-state-restore",
            self.router.execution_state_restore,
        )
        application.router.add_post(
            "/runtime/mcp-playwright-profile/writeback-candidate",
            self.router.writeback_candidate_publish,
        )
        application.router.add_route("*", "/{path:.*}", self.router.request_proxy)
        self.client = TestClient(TestServer(application))

    def profile_path_get(self, physical_profile: str, network_proxy_name: str | None = None) -> Path:
        """Return the exact pair-local profile path used by the router."""

        return self.router._profile_path_get(
            PlaywrightMcpBackendIdentity(
                network_proxy_name=network_proxy_name,
                physical_profile=physical_profile,
            )
        )

    async def close(self) -> None:
        """Close router backends and the public test server."""

        await self.router.close()
        await self.client.close()


def _router_test(run: Callable[[RouterFixture], object], tmp_path: Path) -> None:
    """Run one asynchronous router assertion with deterministic cleanup."""

    async def execute() -> None:
        fixture = RouterFixture(tmp_path)
        await fixture.client.start_server()
        try:
            await run(fixture)
        finally:
            await fixture.close()

    asyncio.run(execute())


def test_named_profile_is_reused_without_source_and_reset_on_each_new_source_session(tmp_path: Path) -> None:
    """Reset on each explicit-source initialization, but not follow-up session requests."""

    async def run(fixture: RouterFixture) -> None:
        source_path = fixture.profile_path_get("source")
        source_path.mkdir(parents=True)
        (source_path / "state.txt").write_text("source-v1", encoding="utf-8")

        response = await fixture.client.post(
            "/mcp?profile=target&profile_source=source",
            json={"jsonrpc": "2.0", "method": "initialize"},
        )
        assert response.status == 200
        assert fixture.profile_path_get("target").joinpath("state.txt").read_text(encoding="utf-8") == "source-v1"
        backend = fixture.backend_by_profile_map["target"]
        assert backend.start_count == 1

        fixture.profile_path_get("target").joinpath("state.txt").write_text("session-state", encoding="utf-8")
        response = await fixture.client.post(
            "/mcp?profile=target&profile_source=source",
            data=b"follow-up",
            headers={"mcp-session-id": "session-1"},
        )
        assert response.status == 200
        assert fixture.profile_path_get("target").joinpath("state.txt").read_text(encoding="utf-8") == "session-state"
        assert backend.stop_count == 0

        response = await fixture.client.post(
            "/mcp?profile=target&profile_source=source",
            json={"jsonrpc": "2.0", "method": "tools/list"},
        )
        assert response.status == 200
        assert fixture.profile_path_get("target").joinpath("state.txt").read_text(encoding="utf-8") == "session-state"
        assert backend.stop_count == 0

        (source_path / "state.txt").write_text("source-v2", encoding="utf-8")
        response = await fixture.client.post(
            "/mcp?profile=target&profile_source=source",
            json={"jsonrpc": "2.0", "method": "initialize"},
        )
        assert response.status == 200
        assert fixture.profile_path_get("target").joinpath("state.txt").read_text(encoding="utf-8") == "source-v2"
        assert backend.stop_count == 1
        assert backend.start_count == 2

    _router_test(run, tmp_path)


def test_source_reset_remains_coupled_to_its_initialize_delivery(tmp_path: Path) -> None:
    """Do not let a later same-target reset overtake the triggering initialize request."""

    async def run(fixture: RouterFixture) -> None:
        first_source_path = fixture.profile_path_get("first-source")
        second_source_path = fixture.profile_path_get("second-source")
        first_source_path.mkdir(parents=True)
        second_source_path.mkdir(parents=True)
        (first_source_path / "state.txt").write_text("first", encoding="utf-8")
        (second_source_path / "state.txt").write_text("second", encoding="utf-8")

        async with ClientSession() as first_client, ClientSession() as second_client:
            first_request = asyncio.create_task(
                first_client.post(
                    fixture.client.make_url("/hold?profile=target&profile_source=first-source"),
                    json={"jsonrpc": "2.0", "method": "initialize"},
                )
            )
            await asyncio.wait_for(fixture.hold_response_headers_sent.wait(), timeout=1)
            first_response = await asyncio.wait_for(first_request, timeout=1)
            target_state_path = fixture.profile_path_get("target") / "state.txt"
            assert target_state_path.read_text(encoding="utf-8") == "first"
            target_backend = fixture.backend_by_profile_map["target"]

            second_request = asyncio.create_task(
                second_client.post(
                    fixture.client.make_url("/mcp?profile=target&profile_source=second-source"),
                    json={"jsonrpc": "2.0", "method": "initialize"},
                )
            )
            await asyncio.sleep(0.05)
            second_reset_blocked = not target_backend.stop_request_started.is_set()

            fixture.hold_response_body_release.set()
            first_body = await first_response.read()
            second_response = await second_request
            assert second_reset_blocked
            assert first_response.status == 200
            assert first_body == b'{"held": true}'
            assert second_response.status == 200
            assert target_state_path.read_text(encoding="utf-8") == "second"

    _router_test(run, tmp_path)


def test_verification_route_reuses_named_backend_without_reset(tmp_path: Path) -> None:
    """Reuse the action backend and physical userDataDir for profile-only verification."""

    async def run(fixture: RouterFixture) -> None:
        source_path = fixture.profile_path_get("source")
        source_path.mkdir(parents=True)
        (source_path / "state.txt").write_text("source", encoding="utf-8")
        action_response = await fixture.client.post("/mcp?profile=target&profile_source=source")
        action_payload = await action_response.json()
        verification_response = await fixture.client.post("/mcp?profile=target")
        verification_payload = await verification_response.json()

        assert action_payload["profile"] == "target"
        assert verification_payload["profile"] == "target"
        assert fixture.backend_by_profile_map["target"].start_count == 1
        assert fixture.backend_by_profile_map["target"].stop_count == 0

    _router_test(run, tmp_path)


def test_named_profile_without_source_materializes_immutable_default_only_once(tmp_path: Path) -> None:
    """Initialize a missing target once without overwriting later run-local state."""

    async def run(fixture: RouterFixture) -> None:
        immutable_source_path = fixture.secret_root_path / "playwright_profile"
        (immutable_source_path / "state.txt").write_text("immutable", encoding="utf-8")

        first_response = await fixture.client.post("/mcp?profile=target")
        target_state_path = fixture.profile_path_get("target") / "state.txt"
        assert first_response.status == 200
        assert target_state_path.read_text(encoding="utf-8") == "immutable"

        target_state_path.write_text("run-local", encoding="utf-8")
        second_response = await fixture.client.post("/mcp?profile=target")
        assert second_response.status == 200
        assert target_state_path.read_text(encoding="utf-8") == "run-local"

    _router_test(run, tmp_path)


def test_named_profile_without_source_rejects_missing_immutable_profile_directory(tmp_path: Path) -> None:
    """Require the secret root profile to exist even when its directory is empty."""

    async def run(fixture: RouterFixture) -> None:
        fixture.secret_root_path.joinpath("playwright_profile").rmdir()

        response = await fixture.client.post("/mcp?profile=target")

        assert response.status == 400
        assert "profile directory is missing" in await response.text()
        assert not fixture.profile_path_get("target").exists()

    _router_test(run, tmp_path)


def test_router_default_candidate_path_is_one_shared_runtime_directory(tmp_path: Path) -> None:
    """Keep the default candidate owner at the exact shared runtime path."""

    backend_config = PlaywrightMcpConfig(
        secret_root_path=tmp_path / "secret-root",
        output_dir=tmp_path / ".playwright-mcp" / "base",
    )

    config = PlaywrightMcpRouterConfig(
        backend_config=backend_config,
        execution_state_restore_token=SecretStr("execution-state-restore-token"),
        network_proxy_config=NetworkProxyConfig(proxy_by_name_map={}),
    )

    assert _DEFAULT_CANDIDATE_ROOT_PATH == Path("/runtime/mcp_playwright_profile/writeback_candidate")
    assert _DEFAULT_EXECUTION_STATE_RESTORE_MARKER_PATH == Path(
        "/runtime/mcp_playwright_profile/execution_state_restore.json"
    )
    assert config.candidate_root_path == _DEFAULT_CANDIDATE_ROOT_PATH
    assert config.execution_state_restore_marker_path == _DEFAULT_EXECUTION_STATE_RESTORE_MARKER_PATH


def test_router_cli_maps_allowed_hosts_to_backend_template_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Parse public router hosts into the canonical backend config field."""

    network_proxy_config_path = tmp_path / "network-proxy.json"
    network_proxy_config_path.write_text('{"proxy_by_name_map": {}}\n', encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "browser-runtime-playwright-mcp-router",
            "--allowed-hosts",
            "localhost,127.0.0.1,browser-mcp",
            "--secret-root-path",
            str(tmp_path / "secret-root"),
            "--network-proxy-config-path",
            str(network_proxy_config_path),
        ],
    )

    namespace = _args_parse()

    assert namespace.allowed_host_list == ["localhost", "127.0.0.1", "browser-mcp"]
    assert namespace.network_proxy_config_path == network_proxy_config_path
    assert "network_proxy_url" not in vars(namespace)
    assert "allowed_hosts" not in vars(namespace)


def test_router_cli_defaults_to_direct_browser_egress(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Accept an empty immutable proxy map for direct browser egress."""

    network_proxy_config_path = tmp_path / "network-proxy.json"
    network_proxy_config_path.write_text('{"proxy_by_name_map": {}}\n', encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "browser-runtime-playwright-mcp-router",
            "--secret-root-path",
            str(tmp_path / "secret-root"),
            "--network-proxy-config-path",
            str(network_proxy_config_path),
        ],
    )

    namespace = _args_parse()

    assert namespace.network_proxy_config_path == network_proxy_config_path
    assert "network_proxy_url" not in vars(namespace)


def test_candidate_endpoint_stops_backend_and_atomically_replaces_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stop the selected backend before publishing its exact candidate snapshot."""

    async def run(fixture: RouterFixture) -> None:
        from browser_runtime import playwright_mcp_router

        profile_snapshot = playwright_mcp_router.playwright_profile_snapshot

        def snapshot_record(**kwargs: Path) -> object:
            """Record snapshot ordering before delegating to the atomic helper."""

            fixture.event_list.append("snapshot:target")
            return profile_snapshot(**kwargs)

        monkeypatch.setattr(playwright_mcp_router, "playwright_profile_snapshot", snapshot_record)
        await fixture.client.post("/mcp?profile=target")
        target_path = fixture.profile_path_get("target")
        (target_path / "state.txt").write_text("runtime", encoding="utf-8")
        candidate_path = fixture.candidate_root_path
        candidate_path.mkdir(parents=True)
        (candidate_path / "state.txt").write_text("old", encoding="utf-8")

        response = await fixture.client.post(
            "/runtime/mcp-playwright-profile/writeback-candidate?profile=target",
            data=b"",
        )

        assert response.status == 204
        assert fixture.backend_by_profile_map["target"].stop_count == 1
        assert candidate_path.joinpath("state.txt").read_text(encoding="utf-8") == "runtime"
        assert fixture.event_list.index("stop:target") < fixture.event_list.index("snapshot:target")

        restart_response = await fixture.client.post("/mcp?profile=target")
        assert restart_response.status == 200
        assert fixture.backend_by_profile_map["target"].start_count == 2

    _router_test(run, tmp_path)


def test_candidate_endpoint_logs_completion_after_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Emit the structured publication event only after the snapshot completes."""

    async def run(fixture: RouterFixture) -> None:
        from browser_runtime import playwright_mcp_router

        event_list: list[str] = []
        payload_list: list[dict[str, str]] = []
        profile_snapshot = playwright_mcp_router.playwright_profile_snapshot

        def snapshot_record(**kwargs: Path) -> object:
            """Record completed snapshot work before returning to the router."""

            state = profile_snapshot(**kwargs)
            event_list.append("snapshot:completed")
            return state

        def log_record(message: str, *, flush: bool) -> None:
            """Record the structured router event and its ordering."""

            assert flush
            event_list.append("event:logged")
            payload_list.append(json.loads(message))

        monkeypatch.setattr(playwright_mcp_router, "playwright_profile_snapshot", snapshot_record)
        monkeypatch.setattr(builtins, "print", log_record)
        await fixture.client.post("/mcp?profile=target")

        response = await fixture.client.post(
            "/runtime/mcp-playwright-profile/writeback-candidate?profile=target",
            data=b"",
        )

        assert response.status == 204
        assert event_list == ["snapshot:completed", "event:logged"]
        assert len(payload_list) == 1
        assert (
            payload_list[0]["event_name"]
            == "browser_runtime.playwright_mcp_router.writeback_candidate_publication_completed"
        )
        assert payload_list[0]["physical_profile"] == "target"
        assert datetime.fromisoformat(payload_list[0]["completed_at"]).utcoffset() == timezone.utc.utcoffset(None)

    _router_test(run, tmp_path)


def test_candidate_endpoint_replaces_one_shared_candidate_with_latest_profile(tmp_path: Path) -> None:
    """Publish every selected profile into the same last-completion-wins directory."""

    async def run(fixture: RouterFixture) -> None:
        await fixture.client.post("/mcp?profile=first")
        first_profile_path = fixture.profile_path_get("first")
        (first_profile_path / "first.txt").write_text("first", encoding="utf-8")
        first_response = await fixture.client.post("/runtime/mcp-playwright-profile/writeback-candidate?profile=first")

        assert first_response.status == 204
        assert fixture.candidate_root_path.joinpath("first.txt").read_text(encoding="utf-8") == "first"

        await fixture.client.post("/mcp?profile=second")
        second_profile_path = fixture.profile_path_get("second")
        (second_profile_path / "second.txt").write_text("second", encoding="utf-8")
        second_response = await fixture.client.post(
            "/runtime/mcp-playwright-profile/writeback-candidate?profile=second"
        )

        assert second_response.status == 204
        assert fixture.candidate_root_path.joinpath("second.txt").read_text(encoding="utf-8") == "second"
        assert not fixture.candidate_root_path.joinpath("first.txt").exists()
        assert not (fixture.candidate_root_path / "first").exists()
        assert not (fixture.candidate_root_path / "second").exists()

    _router_test(run, tmp_path)


def test_candidate_endpoint_rejects_nonempty_body_and_duplicate_profile(tmp_path: Path) -> None:
    """Require the exact candidate endpoint query and an empty request body."""

    async def run(fixture: RouterFixture) -> None:
        body_response = await fixture.client.post(
            "/runtime/mcp-playwright-profile/writeback-candidate?profile=target",
            data=b"unexpected",
        )
        duplicate_response = await fixture.client.post(
            "/runtime/mcp-playwright-profile/writeback-candidate?profile=one&profile=two",
            data=b"",
        )

        assert body_response.status == 400
        assert duplicate_response.status == 400
        assert fixture.backend_by_profile_map == {}

    _router_test(run, tmp_path)


def test_execution_state_restore_discards_attempt_local_browser_state(tmp_path: Path) -> None:
    """Restore the next execution from the current immutable accepted source."""

    async def run(fixture: RouterFixture) -> None:
        response = await fixture.client.post("/mcp?profile=target")
        assert response.status == 200
        backend = fixture.backend_by_profile_map["target"]
        fixture.profile_path_get("target").joinpath("unaccepted.txt").write_text(
            "unaccepted",
            encoding="utf-8",
        )
        for root_path in [
            fixture.router.config.backend_runtime_root_path,
            fixture.router.config.candidate_root_path,
            fixture.router.config.output_root_path,
        ]:
            root_path.mkdir(parents=True, exist_ok=True)
            root_path.joinpath("stale.txt").write_text("stale", encoding="utf-8")
        fixture.secret_root_path.joinpath("playwright_profile", "accepted.txt").write_text(
            "accepted",
            encoding="utf-8",
        )

        restore_response = await fixture.client.post(
            "/runtime/execution-state-restore",
            headers={
                _EXECUTION_STATE_RESTORE_IDENTITY_HEADER: "execution-1",
                _EXECUTION_STATE_RESTORE_TOKEN_HEADER: "execution-state-restore-token",
            },
        )

        assert restore_response.status == 204
        assert backend.stop_count == 1
        assert fixture.router._backend_by_identity_map == {}
        assert fixture.router._backend_port_set == set()
        marker_payload = json.loads(fixture.execution_state_restore_marker_path.read_text(encoding="utf-8"))
        assert marker_payload["restore_identity"] == "execution-1"
        assert len(marker_payload["signature"]) == 64
        for root_path in [
            fixture.router.config.backend_runtime_root_path,
            fixture.router.config.candidate_root_path,
            fixture.router.config.output_root_path,
            fixture.router.config.profile_root_path,
        ]:
            assert root_path.is_dir()
            assert list(root_path.iterdir()) == []

        resumed_response = await fixture.client.post("/mcp?profile=target")

        assert resumed_response.status == 200
        resumed_profile_path = fixture.profile_path_get("target")
        assert resumed_profile_path.joinpath("accepted.txt").read_text(encoding="utf-8") == "accepted"
        assert not resumed_profile_path.joinpath("unaccepted.txt").exists()

    _router_test(run, tmp_path)


def test_execution_state_restore_is_idempotent_for_one_execution_identity(tmp_path: Path) -> None:
    """Do not erase state again when the controller repeats one completed request."""

    async def run(fixture: RouterFixture) -> None:
        header_by_name_map = {
            _EXECUTION_STATE_RESTORE_IDENTITY_HEADER: "execution-1",
            _EXECUTION_STATE_RESTORE_TOKEN_HEADER: "execution-state-restore-token",
        }
        first_response = await fixture.client.post(
            "/runtime/execution-state-restore",
            headers=header_by_name_map,
        )
        assert first_response.status == 204
        fixture.candidate_root_path.mkdir(parents=True, exist_ok=True)
        repeated_state_path = fixture.candidate_root_path / "repeated-state.txt"
        repeated_state_path.write_text("preserved", encoding="utf-8")

        repeated_response = await fixture.client.post(
            "/runtime/execution-state-restore",
            headers=header_by_name_map,
        )

        assert repeated_response.status == 204
        assert repeated_state_path.read_text(encoding="utf-8") == "preserved"

        forged_marker_payload = json.loads(
            fixture.execution_state_restore_marker_path.read_text(encoding="utf-8")
        )
        forged_marker_payload["restore_identity"] = "execution-2"
        fixture.execution_state_restore_marker_path.write_text(
            json.dumps(forged_marker_payload),
            encoding="utf-8",
        )
        next_response = await fixture.client.post(
            "/runtime/execution-state-restore",
            headers={
                **header_by_name_map,
                _EXECUTION_STATE_RESTORE_IDENTITY_HEADER: "execution-2",
            },
        )
        assert next_response.status == 204
        assert not repeated_state_path.exists()

    _router_test(run, tmp_path)


@pytest.mark.parametrize(
    ("request_path", "request_body", "error_text"),
    [
        (
            "/runtime/execution-state-restore?unexpected=value",
            b"",
            "does not accept query parameters",
        ),
        (
            "/runtime/execution-state-restore",
            b"unexpected",
            "request body must be empty",
        ),
    ],
)
def test_execution_state_restore_rejects_unexpected_input(
    request_path: str,
    request_body: bytes,
    error_text: str,
    tmp_path: Path,
) -> None:
    """Keep the platform restore command narrow and unambiguous."""

    async def run(fixture: RouterFixture) -> None:
        response = await fixture.client.post(
            request_path,
            data=request_body,
            headers={
                _EXECUTION_STATE_RESTORE_IDENTITY_HEADER: "execution-1",
                _EXECUTION_STATE_RESTORE_TOKEN_HEADER: "execution-state-restore-token",
            },
        )

        assert response.status == 400
        assert error_text in await response.text()

    _router_test(run, tmp_path)


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_execution_state_restore_requires_platform_credential(
    token: str | None,
    tmp_path: Path,
) -> None:
    """Reject reset requests that do not carry the exact narrow credential."""

    async def run(fixture: RouterFixture) -> None:
        response = await fixture.client.post(
            "/runtime/execution-state-restore",
            headers={} if token is None else {_EXECUTION_STATE_RESTORE_TOKEN_HEADER: token},
        )

        assert response.status == 403

    _router_test(run, tmp_path)


def test_execution_state_restore_interrupts_stale_in_flight_browser_request(tmp_path: Path) -> None:
    """Interrupt predecessor streams after the platform has proven its stop."""

    async def run(fixture: RouterFixture) -> None:
        async with ClientSession() as browser_client, ClientSession() as restore_client:
            browser_request = asyncio.create_task(browser_client.post(fixture.client.make_url("/hold?profile=target")))
            await asyncio.wait_for(fixture.hold_response_headers_sent.wait(), timeout=1)
            browser_response = await asyncio.wait_for(browser_request, timeout=1)
            backend = fixture.backend_by_profile_map["target"]
            restore_request = asyncio.create_task(
                restore_client.post(
                    fixture.client.make_url("/runtime/execution-state-restore"),
                    headers={
                        _EXECUTION_STATE_RESTORE_IDENTITY_HEADER: "execution-1",
                        _EXECUTION_STATE_RESTORE_TOKEN_HEADER: "execution-state-restore-token",
                    },
                )
            )

            restore_response = await asyncio.wait_for(restore_request, timeout=1)
            assert restore_response.status == 204
            assert backend.stop_request_started.is_set()
            assert backend.stop_count == 1
            browser_response.close()

    _router_test(run, tmp_path)


def test_unprofiled_route_uses_isolated_session_backend(tmp_path: Path) -> None:
    """Use one isolated backend without creating a named persistent profile."""

    async def run(fixture: RouterFixture) -> None:
        response = await fixture.client.post("/mcp", json={"method": "initialize"})

        assert response.status == 200
        backend = fixture.backend_by_profile_map["isolated"]
        assert backend.config.isolated is True
        assert backend.config.persistent_profile_path is None
        assert not fixture.profile_root_path.exists()

    _router_test(run, tmp_path)


def test_named_unprofiled_profile_does_not_collide_with_isolated_backend(tmp_path: Path) -> None:
    """Keep named and isolated backend runtime namespaces distinct."""

    async def run(fixture: RouterFixture) -> None:
        named_response = await fixture.client.post("/mcp?profile=unprofiled")
        isolated_response = await fixture.client.post("/mcp")

        assert named_response.status == 200
        assert isolated_response.status == 200
        named_config = fixture.backend_by_profile_map["unprofiled"].config
        isolated_config = fixture.backend_by_profile_map["isolated"].config
        assert named_config.mcp_config_path != isolated_config.mcp_config_path
        assert named_config.output_dir != isolated_config.output_dir
        assert named_config.persistent_profile_path == fixture.profile_path_get("unprofiled")
        assert isolated_config.persistent_profile_path is None

    _router_test(run, tmp_path)


@pytest.mark.parametrize(
    ("query", "error_text"),
    [
        ("profile=one&profile=two", "profile must occur at most once"),
        ("profile=one&profile_source=first&profile_source=second", "profile_source must occur at most once"),
        ("profile=../unsafe", "profile is unsafe"),
        ("profile_source=source", "profile_source requires profile"),
        ("profile=same&profile_source=same", "profile_source must differ from profile"),
        ("profile=missing&profile_source=source", "source profile is missing"),
    ],
)
def test_invalid_profile_routes_are_rejected(query: str, error_text: str, tmp_path: Path) -> None:
    """Reject ambiguous or unsafe profile selection before backend launch."""

    async def run(fixture: RouterFixture) -> None:
        response = await fixture.client.post(f"/mcp?{query}")

        assert response.status == 400
        assert error_text in await response.text()
        assert fixture.backend_by_profile_map == {}

    _router_test(run, tmp_path)


def test_proxy_preserves_status_body_streaming_headers_and_non_router_query(tmp_path: Path) -> None:
    """Preserve the upstream MCP response while removing only router-owned query fields."""

    async def run(fixture: RouterFixture) -> None:
        async with ClientSession() as session:
            response = await session.post(
                fixture.client.make_url("/stream?profile=target&cursor=next"),
                data=b"request-body",
                headers={"accept": "text/event-stream", "mcp-session-id": "request-session"},
            )
            body = await response.read()

        assert response.status == 207
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["mcp-session-id"] == "session-7"
        assert response.headers["x-upstream"] == "yes"
        assert body == b"event: message\ndata: one\n\n"

        identity_response = await fixture.client.post("/mcp?profile=target&cursor=next", data=b"payload")
        identity_payload = await identity_response.json()
        assert identity_payload["body"] == "payload"
        assert identity_payload["query"] == [["cursor", "next"]]

    _router_test(run, tmp_path)


def test_proxy_releases_upstream_when_downstream_disconnects_during_streaming(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Treat a downstream reset during response writes as normal proxy completion."""

    class DisconnectingStreamResponse:
        """Simulate a prepared downstream response whose transport closes on write."""

        def __init__(self, **kwargs: object) -> None:
            """Accept the aiohttp response constructor contract."""

        async def prepare(self, request: web.Request) -> None:
            """Prepare the simulated downstream response."""

        async def write(self, chunk: bytes) -> None:
            """Raise the exact aiohttp downstream disconnect condition."""

            raise ClientConnectionResetError("Cannot write to closing transport")

        async def write_eof(self) -> None:
            """Fail if finalization is attempted after the disconnected write."""

            raise AssertionError("write_eof must not run after a downstream reset")

    class UpstreamContent:
        """Yield one deterministic upstream response chunk."""

        async def iter_chunked(self, size: int) -> object:
            """Yield one chunk through the upstream streaming contract."""

            yield b"event: message\n\n"

    class UpstreamResponse:
        """Expose the upstream response surface used by the proxy."""

        def __init__(self) -> None:
            """Initialize one unreleased upstream response."""

            self.content = UpstreamContent()
            self.headers: dict[str, str] = {}
            self.reason = "OK"
            self.released = False
            self.status = 200

        def release(self) -> None:
            """Record release of the upstream connection."""

            self.released = True

    monkeypatch.setattr(web, "StreamResponse", DisconnectingStreamResponse)

    async def run(fixture: RouterFixture) -> None:
        upstream_response = UpstreamResponse()

        await fixture.router._backend_response_proxy(
            request=object(),
            upstream_response=upstream_response,
        )

        assert upstream_response.released

    _router_test(run, tmp_path)


def test_proxy_releases_upstream_when_downstream_disconnects_during_finalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Treat a downstream reset from write_eof as normal proxy completion.

    Args:
        monkeypatch: Pytest attribute patch helper.
        tmp_path: Temporary router runtime root.
    """

    class FinalizingStreamResponse:
        """Simulate successful writes followed by a closing transport at finalization."""

        def __init__(self, **kwargs: object) -> None:
            """Accept the aiohttp response constructor contract.

            Args:
                kwargs: Aiohttp response options ignored by the fake.
            """

            self.chunk_list: list[bytes] = []
            self.write_eof_called = False

        async def prepare(self, request: web.Request) -> None:
            """Prepare the simulated downstream response.

            Args:
                request: Downstream request ignored by the fake.
            """

        async def write(self, chunk: bytes) -> None:
            """Record one successfully proxied response chunk.

            Args:
                chunk: Proxied response bytes.
            """

            self.chunk_list.append(chunk)

        async def write_eof(self) -> None:
            """Raise the exact aiohttp finalization disconnect condition."""

            self.write_eof_called = True
            raise ClientConnectionResetError("Cannot write to closing transport")

    class UpstreamContent:
        """Yield one deterministic upstream response chunk."""

        async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
            """Yield one chunk through the upstream streaming contract.

            Args:
                size: Requested maximum chunk size.

            Returns:
                Asynchronous iterator yielding one deterministic response chunk.
            """

            yield b"event: message\n\n"

    class UpstreamResponse:
        """Expose the upstream response surface used by the proxy."""

        def __init__(self) -> None:
            """Initialize one unreleased upstream response."""

            self.content = UpstreamContent()
            self.headers: dict[str, str] = {}
            self.reason = "OK"
            self.released = False
            self.status = 200

        def release(self) -> None:
            """Record release of the upstream connection."""

            self.released = True

    monkeypatch.setattr(web, "StreamResponse", FinalizingStreamResponse)

    async def run(fixture: RouterFixture) -> None:
        """Exercise proxy finalization through the shared router fixture.

        Args:
            fixture: Initialized router fixture.
        """

        upstream_response = UpstreamResponse()
        response = await fixture.router._backend_response_proxy(
            request=object(),
            upstream_response=upstream_response,
        )

        assert response.chunk_list == [b"event: message\n\n"]
        assert response.write_eof_called
        assert upstream_response.released

    _router_test(run, tmp_path)


def test_proxy_client_has_no_total_timeout_for_event_streams(tmp_path: Path) -> None:
    """Keep long-lived MCP event streams outside aiohttp's default total timeout."""

    async def run(fixture: RouterFixture) -> None:
        client_session = fixture.router._client_session_get()

        assert client_session.timeout.total is None

    _router_test(run, tmp_path)


def test_router_enforces_public_allowed_hosts_and_keeps_internal_loopback_hosts(tmp_path: Path) -> None:
    """Reject unknown public Host values without removing backend loopback access."""

    async def execute() -> None:
        fixture = RouterFixture(tmp_path, allowed_host_list=["browser-mcp"])
        await fixture.client.start_server()
        try:
            accepted_response = await fixture.client.post(
                "/mcp?profile=target",
                headers={"host": "browser-mcp:8931"},
            )
            rejected_response = await fixture.client.post(
                "/mcp?profile=target",
                headers={"host": "untrusted.example:8931"},
            )

            assert accepted_response.status == 200
            assert rejected_response.status == 400
            backend_allowed_host_list = fixture.backend_by_profile_map["target"].config.allowed_host_list
            assert backend_allowed_host_list == ["browser-mcp", "localhost", "127.0.0.1"]
        finally:
            await fixture.close()

    asyncio.run(execute())


def test_backend_local_paths_and_ports_are_disjoint(tmp_path: Path) -> None:
    """Allocate separate config, output, profile, and TCP namespaces per backend."""

    async def run(fixture: RouterFixture) -> None:
        await fixture.client.post("/mcp?profile=first")
        await fixture.client.post("/mcp?profile=second")
        await fixture.client.post("/mcp")

        config_list = [backend.config for backend in fixture.backend_by_profile_map.values()]
        assert len({config.mcp_config_path for config in config_list}) == 3
        assert len({config.output_dir for config in config_list}) == 3
        assert len({config.port for config in config_list}) == 3
        assert fixture.backend_by_profile_map["first"].config.persistent_profile_path == fixture.profile_path_get(
            "first"
        )
        assert fixture.backend_by_profile_map["second"].config.persistent_profile_path == fixture.profile_path_get(
            "second"
        )
        assert fixture.backend_by_profile_map["isolated"].config.persistent_profile_path is None

    _router_test(run, tmp_path)


def test_backend_config_generation_validates_internal_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject an invalid generated port through the canonical config constructor."""

    from browser_runtime import playwright_mcp_router

    monkeypatch.setattr(playwright_mcp_router, "_loopback_port_get", lambda: 0)

    async def run(fixture: RouterFixture) -> None:
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            fixture.router._backend_config_get(
                PlaywrightMcpBackendIdentity(network_proxy_name=None, physical_profile="target")
            )

    _router_test(run, tmp_path)


def test_backend_port_allocation_retries_router_local_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reserve unique ports when the OS allocator repeats an active router port."""

    from browser_runtime import playwright_mcp_router

    port_iterator = iter([12001, 12001, 12002])
    monkeypatch.setattr(playwright_mcp_router, "_loopback_port_get", lambda: next(port_iterator))

    async def run(fixture: RouterFixture) -> None:
        first_config = fixture.router._backend_config_get(
            PlaywrightMcpBackendIdentity(network_proxy_name=None, physical_profile="first")
        )
        second_config = fixture.router._backend_config_get(
            PlaywrightMcpBackendIdentity(network_proxy_name=None, physical_profile="second")
        )

        assert first_config.port == 12001
        assert second_config.port == 12002

    _router_test(run, tmp_path)


def test_unknown_network_proxy_name_is_rejected_before_backend_launch(tmp_path: Path) -> None:
    """Reject exact proxy names that are absent from the immutable platform map."""

    async def run(fixture: RouterFixture) -> None:
        response = await fixture.client.post("/mcp?network_proxy_name=user-a/unknown&profile=target")

        assert response.status == 400
        assert "network proxy is unavailable: user-a/unknown" in await response.text()
        assert fixture.backend_list == []

    _router_test(run, tmp_path)


def test_same_profile_with_different_exact_proxies_uses_independent_backends(tmp_path: Path) -> None:
    """Keep backend lifecycle, filesystem paths, ports, and SOCKS endpoints pair-local."""

    async def run(fixture: RouterFixture) -> None:
        first_response = await fixture.client.post("/mcp?network_proxy_name=user-a/proxy_a&profile=target&cursor=first")
        second_response = await fixture.client.post(
            "/mcp?network_proxy_name=user-b/proxy_b&profile=target&cursor=second"
        )
        first_payload = await first_response.json()
        second_payload = await second_response.json()

        assert first_payload["query"] == [["cursor", "first"]]
        assert second_payload["query"] == [["cursor", "second"]]
        assert len(fixture.backend_list) == 2
        config_by_proxy_url_map = {backend.config.network_proxy_url: backend.config for backend in fixture.backend_list}
        first_config = config_by_proxy_url_map["socks5://proxy-a:1080"]
        second_config = config_by_proxy_url_map["socks5://proxy-b:1080"]
        assert first_config.mcp_config_path != second_config.mcp_config_path
        assert first_config.output_dir != second_config.output_dir
        assert first_config.persistent_profile_path == fixture.profile_path_get("target", "user-a/proxy_a")
        assert second_config.persistent_profile_path == fixture.profile_path_get("target", "user-b/proxy_b")
        assert first_config.port != second_config.port

    _router_test(run, tmp_path)


def test_different_proxy_pair_is_not_blocked_by_held_same_profile_request(tmp_path: Path) -> None:
    """Avoid serializing different proxy identities merely because the physical profile name matches."""

    async def run(fixture: RouterFixture) -> None:
        async with ClientSession() as first_client:
            first_request = asyncio.create_task(
                first_client.post(fixture.client.make_url("/hold?network_proxy_name=user-a/proxy_a&profile=target"))
            )
            await asyncio.wait_for(fixture.hold_response_headers_sent.wait(), timeout=1)
            first_response = await asyncio.wait_for(first_request, timeout=1)

            second_response = await asyncio.wait_for(
                fixture.client.post("/mcp?network_proxy_name=user-b/proxy_b&profile=target"),
                timeout=1,
            )
            assert second_response.status == 200

            fixture.hold_response_body_release.set()
            assert await first_response.read() == b'{"held": true}'

    _router_test(run, tmp_path)


def test_profile_source_reset_stays_within_selected_proxy_identity(tmp_path: Path) -> None:
    """Copy only the source profile bound to the exact selected network proxy."""

    async def run(fixture: RouterFixture) -> None:
        selected_source_path = fixture.profile_path_get("source", "user-a/proxy_a")
        other_source_path = fixture.profile_path_get("source", "user-b/proxy_b")
        selected_source_path.mkdir(parents=True)
        other_source_path.mkdir(parents=True)
        selected_source_path.joinpath("state.txt").write_text("selected", encoding="utf-8")
        other_source_path.joinpath("state.txt").write_text("other", encoding="utf-8")

        response = await fixture.client.post(
            "/mcp?network_proxy_name=user-a/proxy_a&profile=target&profile_source=source",
            json={"jsonrpc": "2.0", "method": "initialize"},
        )

        assert response.status == 200
        assert (
            fixture.profile_path_get("target", "user-a/proxy_a").joinpath("state.txt").read_text(encoding="utf-8")
            == "selected"
        )
        assert not fixture.profile_path_get("target", "user-b/proxy_b").exists()

    _router_test(run, tmp_path)
