"""Tests fuer DockerSubprocessAutoLoginRunner.

Pruefen: korrekter Befehlsaufbau, Env-Mapping (Klartext-Werte landen
nicht in den CLI-Argumenten), Exit-Code-Mapping, Timeout-Handling.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from broker_gateway.cp.auto_login_runner import (
    DockerSubprocessAutoLoginRunner,
    DockerSubprocessConfig,
)


@pytest.fixture
def config() -> DockerSubprocessConfig:
    return DockerSubprocessConfig(
        image_tag="broker-gateway-paper-auto-login:test",
        network="broker-gateway-paper_default",
        target_url="http://broker-gateway-paper-cpgateway:5000/",
        timeout_s=5.0,
    )


def _env_lookup(values: dict[str, str]):
    def _lookup(key: str, default: Any = None) -> Any:
        return values.get(key, default)

    return _lookup


# ---- _build_command ----


def test_command_uses_e_var_form_for_credentials(config) -> None:
    """``-e BG_PAPER_USERNAME`` ohne Wert verhindert, dass der Klartext
    in ``ps`` oder Docker-Inspect-Args landet."""
    runner = DockerSubprocessAutoLoginRunner(
        config, env_lookup=_env_lookup({})
    )
    cmd = runner._build_command()
    assert "-e" in cmd
    # Credentials sind als blosser Variable-Name eingehaengt.
    assert "BG_PAPER_USERNAME" in cmd
    assert "BG_PAPER_PASSWORD" in cmd
    # KEIN Klartext-Wert.
    for arg in cmd:
        assert "BG_PAPER_USERNAME=" not in arg
        assert "BG_PAPER_PASSWORD=" not in arg


def test_command_includes_target_url(config) -> None:
    runner = DockerSubprocessAutoLoginRunner(
        config, env_lookup=_env_lookup({})
    )
    cmd = runner._build_command()
    assert any(
        a == f"BG_AUTO_LOGIN_TARGET_URL={config.target_url}" for a in cmd
    )


def test_command_uses_image_tag_as_last_arg(config) -> None:
    runner = DockerSubprocessAutoLoginRunner(
        config, env_lookup=_env_lookup({})
    )
    cmd = runner._build_command()
    assert cmd[-1] == config.image_tag


def test_command_passes_network(config) -> None:
    runner = DockerSubprocessAutoLoginRunner(
        config, env_lookup=_env_lookup({})
    )
    cmd = runner._build_command()
    assert "--network" in cmd
    assert cmd[cmd.index("--network") + 1] == config.network


def test_command_includes_extra_env_keys() -> None:
    cfg = DockerSubprocessConfig(
        image_tag="x",
        network="n",
        target_url="http://broker-gateway-paper-cpgateway:5000/",
        extra_env={"BG_DEBUG": "1"},
    )
    runner = DockerSubprocessAutoLoginRunner(
        cfg, env_lookup=_env_lookup({"BG_DEBUG": "1"})
    )
    cmd = runner._build_command()
    assert "BG_DEBUG" in cmd


# ---- _build_subprocess_env ----


def test_subprocess_env_propagates_credentials(config) -> None:
    runner = DockerSubprocessAutoLoginRunner(
        config,
        env_lookup=_env_lookup(
            {
                "PATH": "/usr/bin",
                "BG_PAPER_USERNAME": "cborlm399",
                "BG_PAPER_PASSWORD": "secret",
            }
        ),
    )
    env = runner._build_subprocess_env()
    assert env["BG_PAPER_USERNAME"] == "cborlm399"
    assert env["BG_PAPER_PASSWORD"] == "secret"
    assert env["PATH"] == "/usr/bin"


def test_subprocess_env_keeps_docker_host(config) -> None:
    runner = DockerSubprocessAutoLoginRunner(
        config, env_lookup=_env_lookup({"DOCKER_HOST": "unix:///var/run/docker.sock"})
    )
    env = runner._build_subprocess_env()
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"


def test_subprocess_env_drops_unrelated_vars(config) -> None:
    """Env soll minimal sein — nichts Zufaelliges weiterreichen."""
    runner = DockerSubprocessAutoLoginRunner(
        config,
        env_lookup=_env_lookup(
            {
                "PATH": "/usr/bin",
                "BG_PAPER_USERNAME": "u",
                "BG_PAPER_PASSWORD": "p",
                "AWS_ACCESS_KEY": "should-not-leak",
                "HOME": "/root",
            }
        ),
    )
    env = runner._build_subprocess_env()
    assert "AWS_ACCESS_KEY" not in env
    assert "HOME" not in env


# ---- run() Exit-Code-Mapping (subprocess gemockt) ----


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> None:
        return None


async def test_run_returns_exit_code_zero_on_success(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = DockerSubprocessAutoLoginRunner(
        config, env_lookup=_env_lookup({"BG_PAPER_USERNAME": "u", "BG_PAPER_PASSWORD": "p"})
    )

    async def fake_create(*args, **kwargs):
        return _FakeProc(returncode=0, stdout=b'{"phase":"done"}\n')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    result = await runner.run()
    assert result.exit_code == 0
    assert result.error is None


async def test_run_passes_through_nonzero_exit(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = DockerSubprocessAutoLoginRunner(
        config, env_lookup=_env_lookup({"BG_PAPER_USERNAME": "u", "BG_PAPER_PASSWORD": "p"})
    )

    async def fake_create(*args, **kwargs):
        return _FakeProc(returncode=2, stderr=b"login refused\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    result = await runner.run()
    assert result.exit_code == 2
    assert result.error == "login refused"


async def test_run_handles_missing_docker_binary(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = DockerSubprocessAutoLoginRunner(
        config, env_lookup=_env_lookup({"BG_PAPER_USERNAME": "u", "BG_PAPER_PASSWORD": "p"})
    )

    async def fake_create(*args, **kwargs):
        raise FileNotFoundError("docker: not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    result = await runner.run()
    assert result.exit_code == 9
    assert "docker binary not found" in (result.error or "")


async def test_run_handles_timeout(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wenn der Sidecar die ``timeout_s``-Grenze ueberschreitet, kill()
    + Exit-Code 9."""

    class _SlowProc(_FakeProc):
        async def communicate(self):
            await asyncio.sleep(10)  # weit ueber dem 5s-Timeout
            return b"", b""

    async def fake_create(*args, **kwargs):
        return _SlowProc(returncode=0)

    runner = DockerSubprocessAutoLoginRunner(
        DockerSubprocessConfig(
            image_tag="x",
            network="n",
            target_url="http://broker-gateway-paper-cpgateway:5000/",
            timeout_s=0.1,
        ),
        env_lookup=_env_lookup({"BG_PAPER_USERNAME": "u", "BG_PAPER_PASSWORD": "p"}),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    result = await runner.run()
    assert result.exit_code == 9
    assert "timed out" in (result.error or "")
