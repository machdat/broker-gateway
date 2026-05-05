"""Echter ``AutoLoginRunner`` fuer Phase B.

Startet das Sidecar-Image via ``docker run --rm`` als Subprocess.
Bewusst nicht ``docker``-Python-SDK: das wuerde eine zusaetzliche
Dependency einfuehren, die der Service sonst nicht braucht. Das
Subprocess hat aber dieselben Sicherheits-Eigenschaften:

- Credentials werden ausschliesslich per ``-e VAR=...`` als
  Environment-Variable hineingereicht; der ``-e VAR``-Form
  (Wert aus aktuellem Env) ist *nicht* in ``ps`` sichtbar.
- ``--rm`` raeumt den Container nach Exit auf — keine Reste.
- Network-Bindung ist explizit gesetzt; das Sidecar erreicht NUR
  das vorgesehene Compose-Netz.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Mapping

from broker_gateway.cp.auto_login_trigger import AutoLoginResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DockerSubprocessConfig:
    """Aufruf-Parameter fuer ``docker run``."""

    image_tag: str
    network: str
    target_url: str
    timeout_s: float = 120.0
    docker_binary: str = "docker"
    extra_env: Mapping[str, str] = field(default_factory=dict)


class DockerSubprocessAutoLoginRunner:
    """``AutoLoginRunner``-Implementation via ``docker run --rm``.

    Liest ``BG_PAPER_USERNAME``/``BG_PAPER_PASSWORD`` aus dem **eigenen**
    Process-Environment (typischerweise von ``/etc/default/broker-
    gateway-paper`` per Compose-``env_file`` gesetzt) und reicht sie
    als ``-e VAR``-Argumente an den Sidecar-Container weiter.
    Wichtig: Diese Form (ohne Wert) sorgt dafuer, dass der Klartext
    nirgends in ``ps`` oder Docker-Inspect-Logs landet — ``docker``
    liest die Werte aus dem aktuellen Env beim Start.
    """

    def __init__(
        self,
        config: DockerSubprocessConfig,
        *,
        env_lookup=None,
    ) -> None:
        self._config = config
        # ``env_lookup`` injizierbar fuer Tests; Default ``os.environ.get``.
        if env_lookup is None:
            import os

            self._env_lookup = os.environ.get
        else:
            self._env_lookup = env_lookup

    def _build_command(self) -> list[str]:
        cmd = [
            self._config.docker_binary,
            "run",
            "--rm",
            "--network",
            self._config.network,
            "-e",
            f"BG_AUTO_LOGIN_TARGET_URL={self._config.target_url}",
            # ``-e VAR`` (kein =Wert) liest den Wert aus dem aktuellen
            # Process-Env, schreibt ihn aber nicht in das Argument-
            # Array. So bleibt der Klartext aus ``ps``-Listings
            # und Docker-Inspect-Argumenten heraus.
            "-e",
            "BG_PAPER_USERNAME",
            "-e",
            "BG_PAPER_PASSWORD",
        ]
        for key in self._config.extra_env:
            cmd.extend(["-e", key])
        cmd.append(self._config.image_tag)
        return cmd

    def _build_subprocess_env(self) -> dict[str, str]:
        """Subprocess-Env: minimal, nur die Vars die Docker braucht."""
        env: dict[str, str] = {}
        for key in (
            "PATH",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            value = self._env_lookup(key)
            if value:
                env[key] = value
        # Credentials weiterreichen, damit ``docker -e VAR`` den Wert
        # findet.
        for key in ("BG_PAPER_USERNAME", "BG_PAPER_PASSWORD"):
            value = self._env_lookup(key)
            if value is None:
                continue
            env[key] = value
        for key, value in self._config.extra_env.items():
            env[key] = value
        return env

    async def run(self) -> AutoLoginResult:
        cmd = self._build_command()
        env = self._build_subprocess_env()
        logger.info(
            "auto-login: starting sidecar (image=%s, network=%s)",
            self._config.image_tag,
            self._config.network,
        )
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return AutoLoginResult(
                exit_code=9,
                duration_s=0.0,
                error=f"docker binary not found: {exc!s}",
            )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self._config.timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration = round(time.monotonic() - started, 2)
            return AutoLoginResult(
                exit_code=9,
                duration_s=duration,
                error=f"sidecar timed out after {self._config.timeout_s}s",
            )

        duration = round(time.monotonic() - started, 2)
        exit_code = proc.returncode if proc.returncode is not None else 9
        # Sidecar-stdout ist strukturierter JSON-Log: nur fuer
        # Logging weiterreichen, NICHT in AutoLoginResult.error
        # speichern (der Aufrufer schaut nur auf den Exit-Code).
        if stdout_bytes:
            for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    logger.info("auto-login sidecar stdout: %s", line)
        error_msg: str | None = None
        if stderr_bytes:
            err_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            if err_text:
                logger.warning("auto-login sidecar stderr: %s", err_text)
                if exit_code != 0:
                    # nur den ersten Abschnitt, damit das Lifecycle-
                    # Log nicht ueberlaeuft.
                    error_msg = err_text.splitlines()[0][:200]
        return AutoLoginResult(
            exit_code=exit_code, duration_s=duration, error=error_msg
        )


__all__ = ["DockerSubprocessAutoLoginRunner", "DockerSubprocessConfig"]
