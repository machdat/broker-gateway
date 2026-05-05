"""Tests fuer die browser-unabhaengige Sidecar-Logik.

Der Browser-Flow selbst (Playwright + Chromium) wird ueber Live-
Smokes auf cma-pi-1 verifiziert; hier pruefen wir nur die Stuecke,
die Klartext-Credentials anfassen oder Sicherheits-Entscheidungen
treffen.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


def _load_logic_module():
    """Laedt ops/auto-login/auto_login_logic.py als Modul.

    Bewusst kein ``sys.path``-Hack: die Datei lebt unter ``ops/`` und
    soll nicht als regulaeres Python-Paket importierbar sein.
    """
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "ops" / "auto-login" / "auto_login_logic.py"
    spec = importlib.util.spec_from_file_location("auto_login_logic", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_login_logic"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def logic():
    return _load_logic_module()


# ---- mask_username ----


def test_mask_username_typical(logic) -> None:
    assert logic.mask_username("cborlm399") == "cb***99"


def test_mask_username_short(logic) -> None:
    assert logic.mask_username("abcd") == "***"
    assert logic.mask_username("ab") == "***"
    assert logic.mask_username("") == ""


def test_mask_username_long(logic) -> None:
    # 5+ Zeichen: 2 vorne, 2 hinten.
    assert logic.mask_username("abcdef") == "ab***ef"
    assert logic.mask_username("longusername") == "lo***me"


# ---- is_paper_target ----


def test_is_paper_target_compose_service_name(logic) -> None:
    assert logic.is_paper_target(
        "http://broker-gateway-paper-cpgateway:5000/"
    ) is True


def test_is_paper_target_legacy_short_name(logic) -> None:
    """Auch der haendisch erwaehnte 'paper-cpgateway' soll matchen,
    falls der Operator ihn so im Compose-Netz aliased."""
    assert logic.is_paper_target("http://paper-cpgateway:5000/") is True


def test_is_paper_target_live_blocked(logic) -> None:
    assert logic.is_paper_target("http://cpgateway:5000/") is False
    assert logic.is_paper_target("http://broker-gateway-cpgateway:5000/") is False


def test_is_paper_target_localhost_blocked(logic) -> None:
    assert logic.is_paper_target("http://localhost:5000/") is False
    assert logic.is_paper_target("http://127.0.0.1:5000/") is False


def test_is_paper_target_empty(logic) -> None:
    assert logic.is_paper_target("") is False


# ---- classify_dispatcher ----


def test_classify_dispatcher_success(logic) -> None:
    assert (
        logic.classify_dispatcher(200, "Client login succeeds")
        == logic.EXIT_OK
    )


def test_classify_dispatcher_success_with_extra_html(logic) -> None:
    body = "<html><body>Client login succeeds</body></html>"
    assert logic.classify_dispatcher(200, body) == logic.EXIT_OK


def test_classify_dispatcher_wrong_body_refused(logic) -> None:
    assert (
        logic.classify_dispatcher(200, "Client login failed")
        == logic.EXIT_LOGIN_REFUSED
    )


def test_classify_dispatcher_empty_body(logic) -> None:
    assert logic.classify_dispatcher(200, "") == logic.EXIT_LOGIN_REFUSED


def test_classify_dispatcher_non_200(logic) -> None:
    assert logic.classify_dispatcher(401, "auth fail") == logic.EXIT_LOGIN_REFUSED
    assert logic.classify_dispatcher(500, "Client login succeeds") == logic.EXIT_LOGIN_REFUSED


# ---- emit_log ----


def test_emit_log_writes_json_with_sorted_keys(logic) -> None:
    buf = io.StringIO()
    line = logic.emit_log(
        logic.JsonLogEvent(phase="start", fields={"target": "url", "username": "cb***99"}),
        stream=buf,
    )
    parsed = json.loads(line)
    assert parsed["phase"] == "start"
    assert parsed["target"] == "url"
    assert parsed["username"] == "cb***99"
    assert "ts" in parsed
    assert buf.getvalue().endswith("\n")


def test_emit_log_does_not_leak_extra_objects(logic) -> None:
    """Stellt sicher, dass keine ungewollten Felder im JSON landen."""
    buf = io.StringIO()
    logic.emit_log(
        logic.JsonLogEvent(phase="done", fields={"exit_code": 0}),
        stream=buf,
    )
    parsed = json.loads(buf.getvalue())
    assert set(parsed.keys()) == {"phase", "exit_code", "ts"}


def test_emit_log_one_line_per_event(logic) -> None:
    """Jeder Event-Aufruf erzeugt genau eine Zeile (wichtig fuer
    journald-Aggregation)."""
    buf = io.StringIO()
    for i in range(3):
        logic.emit_log(
            logic.JsonLogEvent(phase=f"step{i}", fields={"i": i}),
            stream=buf,
        )
    lines = [l for l in buf.getvalue().split("\n") if l]
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # darf nicht werfen


# ---- Konsistenz mit cp/auto_login_trigger.py ----


def test_exit_codes_match_trigger_module(logic) -> None:
    """Die Exit-Codes muessen sich mit AutoLoginResult.exit_code im
    Trigger-Modul decken — siehe Karten-Spec und cp/auto_login_trigger.py."""
    assert logic.EXIT_OK == 0
    assert logic.EXIT_FORM_NOT_FOUND == 1
    assert logic.EXIT_LOGIN_REFUSED == 2
    assert logic.EXIT_NETWORK == 3
    assert logic.EXIT_2FA == 4
    assert logic.EXIT_HARD_GUARD == 5
    assert logic.EXIT_OTHER == 9
