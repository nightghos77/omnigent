"""Tests for the opencode serve process manager + arg/env builders."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent import opencode_native_app_server as appsrv
from omnigent.opencode_native_app_server import (
    OpenCodeCliNotFoundError,
    OpenCodeNativeServer,
    OpenCodeVersionError,
    build_opencode_attach_args,
    build_opencode_serve_args,
    check_opencode_version,
    filtered_server_env,
    find_opencode_cli,
    opencode_terminal_env,
    parse_opencode_version,
)


def test_parse_opencode_version() -> None:
    assert parse_opencode_version("opencode 1.17.7") == "1.17.7"
    assert parse_opencode_version("1.17.7") == "1.17.7"
    assert parse_opencode_version("v1.17.7-beta.1") == "1.17.7-beta.1"
    assert parse_opencode_version("no version here") is None


def test_check_version_in_range() -> None:
    check_opencode_version("1.17.7")
    check_opencode_version("1.17.99")


@pytest.mark.parametrize("version", ["1.16.0", "1.18.0", "2.0.0"])
def test_check_version_out_of_range_raises(version: str) -> None:
    with pytest.raises(OpenCodeVersionError):
        check_opencode_version(version)


def test_check_version_unparsable_raises() -> None:
    with pytest.raises(OpenCodeVersionError):
        check_opencode_version("not-a-version")


def test_find_opencode_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda _name: None)
    with pytest.raises(OpenCodeCliNotFoundError):
        find_opencode_cli()


def test_find_opencode_cli_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert find_opencode_cli() == "/usr/bin/opencode"


def test_build_serve_args_has_explicit_host_port() -> None:
    args = build_opencode_serve_args(hostname="127.0.0.1", port=49231)
    assert args == ["serve", "--hostname", "127.0.0.1", "--port", "49231"]


def test_build_attach_args() -> None:
    args = build_opencode_attach_args(
        server_url="http://127.0.0.1:49231",
        workspace="/repo",
        session_id="ses_1",
    )
    assert args == [
        "attach",
        "http://127.0.0.1:49231",
        "--dir",
        "/repo",
        "--session",
        "ses_1",
    ]


def test_build_attach_args_without_session() -> None:
    args = build_opencode_attach_args(
        server_url="http://127.0.0.1:49231",
        workspace="/repo",
        session_id=None,
        opencode_args=("--extra",),
    )
    assert "--session" not in args
    assert args[-1] == "--extra"


def test_filtered_server_env_sets_xdg_and_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-key")
    monkeypatch.setenv("RANDOM_UNRELATED", "nope")
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert env["XDG_DATA_HOME"] == str(tmp_path / "xdg-data")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw"
    assert env["OPENCODE_SERVER_USERNAME"] == "opencode"
    assert env["ANTHROPIC_API_KEY"] == "secret-key"  # provider env passes through
    assert "RANDOM_UNRELATED" not in env  # unrelated env filtered out


def _server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> OpenCodeNativeServer:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    return OpenCodeNativeServer(
        bridge_dir=tmp_path,
        workspace=tmp_path,
        port=49231,
        verify_version=False,
    )


def test_build_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    server.port = 49231
    argv = server.build_argv()
    assert argv[0] == "/usr/bin/opencode"
    assert argv[1:] == ["serve", "--hostname", "127.0.0.1", "--port", "49231"]


def test_base_url_and_auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    assert server.base_url == "http://127.0.0.1:49231"
    assert server.auth_headers["Authorization"].startswith("Basic ")


def test_terminal_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    env = opencode_terminal_env(server)
    assert env["OPENCODE_SERVER_PASSWORD"] == server.auth_secret
    assert env["XDG_DATA_HOME"] == str(server.xdg_data_home)


async def test_start_polls_until_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    started: dict[str, object] = {}

    class _FakeProc:
        pid = 4242

        def poll(self) -> None:
            return None

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        started["argv"] = argv
        started["env"] = kwargs.get("env")
        return _FakeProc()

    async def fake_wait(self: OpenCodeNativeServer) -> None:
        started["ready"] = True

    monkeypatch.setattr(appsrv.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", fake_wait)
    await server.start()
    assert started["ready"] is True
    assert started["argv"][1] == "serve"
    assert server.process is not None
    assert server.process.pid == 4242
