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
    # Browser-Auswahl seit v1.31.0: Karte 739777a9 hat reproduzierbare
    # Renderer-Crashes von ARM64-Chromium am IBKR-React-Submit dokumentiert
    # (sowohl headless als auch headed mit Xvfb). Firefox-Engine umgeht
    # den Bug. Default bleibt chromium fuer Backward-Compat; Paper-Stack
    # auf cma-pi-1 setzt BG_AUTO_LOGIN_BROWSER=firefox.
    headless = os.environ.get("BG_AUTO_LOGIN_HEADLESS", "1").strip() == "1"
    browser_kind = os.environ.get(
        "BG_AUTO_LOGIN_BROWSER", "chromium"
    ).strip().lower()
    async with async_playwright() as p:
        if browser_kind == "firefox":
            browser = await p.firefox.launch(headless=headless)
        else:
            # Chromium-Args fuer Container/Pi-Umgebung:
            # --no-sandbox: Sidecar laeuft als nicht-root, aber Chromium-
            #   Sandbox braucht entweder /proc/self/loginuid oder CAP_SYS_ADMIN.
            # --disable-dev-shm-usage: Default /dev/shm im Container ist 64MB;
            #   Chromium-Renderer crashed dann beim Form-Submit. Erzwingt
            #   /tmp als Shared-Memory-Verzeichnis (langsamer, aber stabil).
            # --disable-gpu: Pi hat keinen GPU-Pfad fuer headless Chromium.
            # --disable-software-rasterizer: spart Renderer-Ressourcen.
            browser = await p.chromium.launch(
                headless=headless,
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

            # Live/Paper-Toggle: nach einem cpgateway-Container-Recreate
            # ist der Default-Mode "Live", auch wenn der konfigurierte
            # User ein Paper-Account ist. Ohne Toggle-Klick lehnt der
            # Server den Login mit "You have selected the Live Account
            # Mode, but the specified user is a Paper Trading user" ab,
            # ohne /sso/Dispatcher zu triggern. In der Phase-1.b-HAR ist
            # der Wechsel an LOGIN_TYPE=1 -> LOGIN_TYPE=2 sichtbar.
            # Live/Paper-Toggle: nach einem cpgateway-Container-Recreate
            # ist der Default-Mode "Live", auch wenn der konfigurierte
            # User ein Paper-Account ist. Ohne Toggle lehnt der Server
            # mit "You have selected the Live Account Mode, but the
            # specified user is a Paper Trading user" ab.
            #
            # DOM-Struktur (Stand 2026-05-07, Karte 739777a9 verifiziert):
            #   <div class="toggle-wrapper xyz-paperswitch">
            #     <input type="checkbox" id="toggle1" name="paperSwitch"
            #            class="toggle-checkbox xyz-paper-switch" />
            #     <span class="toggle-label toggle-off">Live</span>
            #     <span class="toggle-label toggle-on">Paper</span>
            #   </div>
            #
            # Die Spans sind nur visuelle Labels (kein for=). Span-Click
            # funktioniert in Chromium per JS-Listener, in Firefox nicht
            # zuverlaessig. Stattdessen direkt das Checkbox via Playwright-
            # API setzen — set_checked feuert change/input-Events.
            # Form 0 hat ein verstecktes <input name="loginType"
            # class="xyz-logintype">, das beim Submit den Server-Mode
            # steuert. Wenn paperSwitch nicht checked ist, schreibt das
            # JS-Bundle "1" (Live) hinein; bei checked "2" (Paper).
            # Wir setzen beide Werte direkt: paperSwitch.checked=true
            # und loginType.value="2", plus change/input-Events damit
            # eventuelle Listener informiert sind. Falls der Bundle den
            # loginType-Wert kurz vor Submit ueberschreibt, gewinnt der
            # paperSwitch-State (deshalb beide setzen).
            # Strategie:
            #  a) Mouse-Coordinate-Click auf Span — simuliert echten
            #     Trusted-Event, triggert vermutlich den Bundle-Toggle-
            #     Handler korrekt + dispatched evtl. /sso/Init.
            #  b) Falls (a) nicht greift, fallback: paperSwitch.checked
            #     direkt setzen + change-Event, plus loginType-hidden
            #     auf "2".
            try:
                paper_span = page.locator(
                    '.loginformWrapper .toggle-label.toggle-on'
                ).first
                if await paper_span.count() > 0:
                    box = await paper_span.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        await page.wait_for_timeout(1500)
                        _log("paper_toggle", click="mouse_coord", box=box)
                    else:
                        _log("paper_toggle", click="no_box")

                state = await page.evaluate(
                    """
                    () => {
                        const cb = document.querySelector(
                            '.loginformWrapper input.toggle-checkbox.xyz-paper-switch'
                        );
                        const lt = document.querySelector(
                            '.loginformWrapper input.xyz-logintype'
                        );
                        if (cb && !cb.checked) {
                            cb.checked = true;
                            cb.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                        if (lt && lt.value !== '2') {
                            lt.value = '2';
                            lt.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                        return {
                            cb_checked: cb ? cb.checked : null,
                            lt_value: lt ? lt.value : null,
                        };
                    }
                    """
                )
                _log("paper_toggle_state", state=state)
                await page.wait_for_timeout(400)
            except Exception as exc:  # noqa: BLE001
                _log("paper_toggle_error", error=str(exc))

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
                try:
                    final_url = page.url
                    page_snippet = await page.evaluate(
                        "document.body ? document.body.innerText.slice(0, 600) : ''"
                    )
                except Exception:  # noqa: BLE001
                    final_url, page_snippet = "<unknown>", ""
                _log(
                    "submit_timeout",
                    final_url=final_url,
                    page_snippet=page_snippet,
                )
                return EXIT_LOGIN_REFUSED, "no /sso/Dispatcher response within timeout"
            except Exception as exc:  # noqa: BLE001
                return EXIT_OTHER, f"submit failed: {exc!s}"

            try:
                body = await response.text()
            except Exception as exc:  # noqa: BLE001
                return EXIT_OTHER, f"reading dispatcher body failed: {exc!s}"

            initial_exit_code = classify_dispatcher(response.status, body)

            # Reload-Trick: User-Erfahrung sagt, dass ein Reload nach
            # erfolgreichem Submit die "Client login succeeds"-Page
            # zeigt — die initial-Dispatcher-Response liefert das nicht.
            # Wir machen drei Probes, weil cpgateway-Status unter-
            # schiedliche Fenster hat:
            #   1) Reload der Login-URL (zeigt "Client login succeeds"
            #      wenn Session aktiv)
            #   2) auth/status mit Cookies aus Browser-Context — 200
            #      + authenticated=true ist der saubere Beweis. 401
            #      kann iserver-Bridge-Drift sein (siehe Memory
            #      project_iserver_bridge_drift), der Login dennoch ok.
            #   3) tickle mit Cookies — Sekundaer-Health-Indikator.
            try:
                await page.wait_for_timeout(1500)
                reload_resp = await page.goto(
                    target_url.rstrip("/") + "/sso/Login?forwardTo=22",
                    timeout=_GOTO_TIMEOUT_MS,
                )
                reload_text = await page.evaluate(
                    "document.body ? document.body.innerText.slice(0, 800) : ''"
                )
                _log(
                    "reload_probe",
                    status=reload_resp.status if reload_resp else None,
                    body_text=reload_text,
                )
                if "Client login succeeds" in (reload_text or ""):
                    return EXIT_OK, None
            except Exception as exc:  # noqa: BLE001
                _log("reload_probe_error", error=str(exc))

            try:
                status_resp = await page.goto(
                    target_url.rstrip("/") + "/v1/api/iserver/auth/status",
                    timeout=_GOTO_TIMEOUT_MS,
                )
                status_text = await page.evaluate(
                    "document.body ? document.body.innerText.slice(0, 600) : ''"
                )
                _log(
                    "auth_probe",
                    status=status_resp.status if status_resp else None,
                    body_text=status_text,
                )
                if status_resp and status_resp.status == 200 and (
                    '"authenticated":true' in (status_text or "")
                    or '"authenticated": true' in (status_text or "")
                ):
                    return EXIT_OK, None
            except Exception as exc:  # noqa: BLE001
                _log("auth_probe_error", error=str(exc))

            if initial_exit_code != EXIT_OK:
                body_str = body or ""
                tokens = [
                    "Client login succeeds",
                    "Live Account Mode",
                    "Paper Trading",
                    "Login failed",
                    "incorrect",
                    "captcha",
                    "Captcha",
                    "Refused",
                    "Locked",
                ]
                hits = []
                for tk in tokens:
                    idx = body_str.find(tk)
                    if idx >= 0:
                        start = max(0, idx - 80)
                        end = min(len(body_str), idx + 200)
                        hits.append({"tok": tk, "ctx": body_str[start:end]})
                _log(
                    "dispatcher_body",
                    status=response.status,
                    body_len=len(body_str),
                    body_head=body_str[:300],
                    body_tail=body_str[-300:] if len(body_str) > 300 else "",
                    hits=hits,
                )
            return initial_exit_code, None
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
