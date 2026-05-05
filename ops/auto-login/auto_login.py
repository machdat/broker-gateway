"""Sidecar-Skript: Auto-Login fuer den Paper-cpgateway.

Wird vom broker-gateway-paper Hauptcontainer via
``docker run --rm broker-gateway-paper-auto-login:<version>`` gestartet.
Liest Credentials und Ziel-URL aus Environment-Variablen, fuehrt den
SRP-6-Login (siehe ``docs/research/cpgateway-login-flow.md``) per
Playwright/Chromium durch und exitet mit definiertem Code.

Exit-Codes (ausgelagert in auto_login_logic, hier nur als Verweis):
  0 Erfolg
  1 Form nicht gefunden / Selector-Drift
  2 Login abgelehnt (Credentials oder Captcha)
  3 paper-cpgateway nicht erreichbar
  4 2FA-Pflicht erkannt — IBKR-Policy-Aenderung
  5 Hard-Guard: Ziel-URL enthaelt nicht `paper-cpgateway`
  9 Anderer Fehler

Bewusst kein argparse: Konfiguration ausschliesslich ueber Env, damit
Credentials nie als CLI-Argument auftauchen (waeren in `ps` lesbar).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback

from playwright.async_api import (  # type: ignore[import-not-found]
    Page,
    Response,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from auto_login_logic import (
    EXIT_2FA,
    EXIT_FORM_NOT_FOUND,
    EXIT_HARD_GUARD,
    EXIT_LOGIN_REFUSED,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_OTHER,
    JsonLogEvent,
    classify_dispatcher,
    emit_log,
    is_paper_target,
    mask_username,
)


_DEFAULT_TARGET_URL = "http://broker-gateway-paper-cpgateway:5000/"
_FORM_WAIT_TIMEOUT_MS = 20_000
_DISPATCHER_WAIT_TIMEOUT_MS = 30_000
_GOTO_TIMEOUT_MS = 15_000


def _log(phase: str, **fields: object) -> None:
    emit_log(JsonLogEvent(phase=phase, fields=dict(fields)))


async def _detect_2fa(page: Page) -> bool:
    """Heuristik: zusaetzliches OTP-/Code-Feld oder /sso/2fa-URL?"""
    try:
        twofa_inputs = await page.locator(
            '.loginformWrapper input[name="otp"], '
            '.loginformWrapper input[name="code"], '
            '.loginformWrapper input[autocomplete="one-time-code"]'
        ).count()
    except Exception:  # noqa: BLE001 — defensive
        twofa_inputs = 0
    if twofa_inputs > 0:
        return True
    if "/sso/2fa" in page.url.lower():
        return True
    return False


async def run_login(
    target_url: str, username: str, password: str
) -> tuple[int, str | None]:
    """Vollstaendiger Login-Flow. Keine Klartext-Credentials in Logs."""
    async with async_playwright() as p:
        # Chromium-Args fuer Container/Pi-Umgebung:
        # --no-sandbox: Sidecar laeuft als nicht-root, aber Chromium-
        #   Sandbox braucht entweder /proc/self/loginuid oder CAP_SYS_ADMIN.
        # --disable-dev-shm-usage: Default /dev/shm im Container ist 64MB;
        #   Chromium-Renderer crashed dann beim Form-Submit. Erzwingt
        #   /tmp als Shared-Memory-Verzeichnis (langsamer, aber stabil).
        # --disable-gpu: Pi hat keinen GPU-Pfad fuer headless Chromium.
        # --disable-software-rasterizer: spart Renderer-Ressourcen.
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
            ],
        )
        context = await browser.new_context()
        page = await context.new_page()
        try:
            try:
                await page.goto(target_url, timeout=_GOTO_TIMEOUT_MS)
            except PlaywrightTimeout:
                return EXIT_NETWORK, "goto timeout"
            except Exception as exc:  # noqa: BLE001
                return EXIT_NETWORK, f"goto failed: {exc!s}"

            # Auf Form-Render warten (das xyz-Bundle injiziert nach
            # Asset-Load).
            try:
                await page.wait_for_selector(
                    '.loginformWrapper input[type="password"]',
                    timeout=_FORM_WAIT_TIMEOUT_MS,
                )
            except PlaywrightTimeout:
                return EXIT_FORM_NOT_FOUND, "loginformWrapper password input not visible"

            if await _detect_2fa(page):
                return EXIT_2FA, "2fa indicator detected before submit"

            try:
                await page.locator(
                    '.loginformWrapper input[type="text"]'
                ).first.fill(username)
                await page.locator(
                    '.loginformWrapper input[type="password"]'
                ).first.fill(password)
            except Exception as exc:  # noqa: BLE001
                return EXIT_FORM_NOT_FOUND, f"fill failed: {exc!s}"

            try:
                async with page.expect_response(
                    lambda r: "/sso/Dispatcher" in r.url,
                    timeout=_DISPATCHER_WAIT_TIMEOUT_MS,
                ) as resp_ctx:
                    submit = page.locator(
                        '.loginformWrapper button[type="submit"], '
                        '.loginformWrapper input[type="submit"]'
                    ).first
                    if await submit.count() > 0:
                        await submit.click()
                    else:
                        # Fallback: Enter im Passwort-Feld.
                        await page.locator(
                            '.loginformWrapper input[type="password"]'
                        ).first.press("Enter")
                response: Response = await resp_ctx.value
            except PlaywrightTimeout:
                # Wenn nach dem Submit pluetzlich ein 2FA-Feld erscheint,
                # ist das auch der 2FA-Failure-Mode.
                if await _detect_2fa(page):
                    return EXIT_2FA, "2fa indicator detected after submit"
                return EXIT_LOGIN_REFUSED, "no /sso/Dispatcher response within timeout"
            except Exception as exc:  # noqa: BLE001
                return EXIT_OTHER, f"submit failed: {exc!s}"

            try:
                body = await response.text()
            except Exception as exc:  # noqa: BLE001
                return EXIT_OTHER, f"reading dispatcher body failed: {exc!s}"

            return classify_dispatcher(response.status, body), None
        finally:
            try:
                await context.close()
            finally:
                await browser.close()


def _read_config() -> tuple[str, str, str] | int:
    """Liest Env-Vars; gibt (target, user, password) oder einen Exit-Code."""
    target = os.environ.get("BG_AUTO_LOGIN_TARGET_URL", _DEFAULT_TARGET_URL).strip()
    user = os.environ.get("BG_PAPER_USERNAME", "").strip()
    pwd = os.environ.get("BG_PAPER_PASSWORD", "")
    if not user or not pwd:
        _log(
            "config",
            error="missing credentials",
            username_set=bool(user),
            password_set=bool(pwd),
        )
        return EXIT_OTHER
    if not is_paper_target(target):
        _log("hard_guard", error="target_not_paper", target=target)
        return EXIT_HARD_GUARD
    return target, user, pwd


async def main() -> int:
    cfg = _read_config()
    if isinstance(cfg, int):
        return cfg
    target, user, pwd = cfg
    _log("start", target=target, username=mask_username(user))
    started = time.monotonic()
    try:
        code, error = await run_login(target, user, pwd)
    except Exception as exc:  # noqa: BLE001 — catch-all top level
        _log(
            "crash",
            error=type(exc).__name__,
            message=str(exc),
            traceback="".join(traceback.format_exception_only(type(exc), exc)),
        )
        return EXIT_OTHER
    duration = round(time.monotonic() - started, 2)
    _log("done", exit_code=code, duration_s=duration, error=error or "")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
