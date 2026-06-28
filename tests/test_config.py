"""Tests fuer broker_gateway.config — Stack-Kind + Auto-Login-Schalter.

BG_STACK_KIND ist beim Startup Pflicht; ein fehlender oder ungueltiger
Wert muss zum Startup-Fail fuehren. Hard-Guard 1 (live + Auto-Login
gleichzeitig) wird hier ebenfalls gepruef — die Test-Erwartung ist
ConfigError mit erkennbarer Fehlermeldung.
"""
from __future__ import annotations

import pytest

from broker_gateway.config import (
    ConfigError,
    StackKind,
    paper_auto_login_enabled,
    paper_credentials,
    quotes_source,
    stack_kind,
    tws_read_only,
    validate_runtime_config,
)


# ---- BG_STACK_KIND ----


def test_stack_kind_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "live")
    assert stack_kind() == "live"


def test_stack_kind_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "paper")
    assert stack_kind() == "paper"


def test_stack_kind_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "PAPER")
    assert stack_kind() == "paper"


def test_stack_kind_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "  live  ")
    assert stack_kind() == "live"


def test_stack_kind_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_STACK_KIND", raising=False)
    with pytest.raises(ConfigError) as ei:
        stack_kind()
    assert "BG_STACK_KIND" in str(ei.value)


def test_stack_kind_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "")
    with pytest.raises(ConfigError):
        stack_kind()


def test_stack_kind_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "production")
    with pytest.raises(ConfigError) as ei:
        stack_kind()
    msg = str(ei.value)
    assert "production" in msg
    assert "live" in msg and "paper" in msg


# ---- BG_PAPER_AUTO_LOGIN ----


def test_paper_auto_login_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_PAPER_AUTO_LOGIN", raising=False)
    assert paper_auto_login_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_paper_auto_login_truthy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("BG_PAPER_AUTO_LOGIN", raw)
    assert paper_auto_login_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", ""])
def test_paper_auto_login_falsy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("BG_PAPER_AUTO_LOGIN", raw)
    assert paper_auto_login_enabled() is False


# ---- BG_PAPER_USERNAME / BG_PAPER_PASSWORD ----


def test_paper_credentials_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_PAPER_USERNAME", "cborlm399")
    monkeypatch.setenv("BG_PAPER_PASSWORD", "secret")
    assert paper_credentials() == ("cborlm399", "secret")


def test_paper_credentials_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_PAPER_USERNAME", raising=False)
    monkeypatch.delenv("BG_PAPER_PASSWORD", raising=False)
    assert paper_credentials() is None


def test_paper_credentials_partial_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_PAPER_USERNAME", "cborlm399")
    monkeypatch.delenv("BG_PAPER_PASSWORD", raising=False)
    assert paper_credentials() is None


# ---- validate_runtime_config (Hard-Guard 1) ----


def test_validate_paper_auto_login_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "paper")
    monkeypatch.setenv("BG_PAPER_AUTO_LOGIN", "0")
    validate_runtime_config()  # darf nicht werfen


def test_validate_paper_auto_login_enabled_with_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "paper")
    monkeypatch.setenv("BG_PAPER_AUTO_LOGIN", "1")
    monkeypatch.setenv("BG_PAPER_USERNAME", "cborlm399")
    monkeypatch.setenv("BG_PAPER_PASSWORD", "secret")
    validate_runtime_config()


def test_validate_live_no_auto_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "live")
    monkeypatch.delenv("BG_PAPER_AUTO_LOGIN", raising=False)
    monkeypatch.delenv("BG_PAPER_USERNAME", raising=False)
    monkeypatch.delenv("BG_PAPER_PASSWORD", raising=False)
    validate_runtime_config()


def test_validate_live_with_auto_login_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-Guard 1: live + auto-login gleichzeitig -> Startup-Fail."""
    monkeypatch.setenv("BG_STACK_KIND", "live")
    monkeypatch.setenv("BG_PAPER_AUTO_LOGIN", "1")
    with pytest.raises(ConfigError) as ei:
        validate_runtime_config()
    msg = str(ei.value)
    assert "live" in msg.lower()
    assert "auto" in msg.lower() or "auto_login" in msg.lower() or "BG_PAPER_AUTO_LOGIN" in msg


def test_validate_live_with_paper_credentials_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard-Guard 1: Paper-Credentials im Live-Stack sind ein
    Konfigurationsfehler — auch wenn Auto-Login deaktiviert ist."""
    monkeypatch.setenv("BG_STACK_KIND", "live")
    monkeypatch.delenv("BG_PAPER_AUTO_LOGIN", raising=False)
    monkeypatch.setenv("BG_PAPER_USERNAME", "cborlm399")
    monkeypatch.setenv("BG_PAPER_PASSWORD", "secret")
    with pytest.raises(ConfigError):
        validate_runtime_config()


def test_validate_paper_auto_login_without_creds_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-Login aktiv ohne Credentials = sicheres Fail-Loud."""
    monkeypatch.setenv("BG_STACK_KIND", "paper")
    monkeypatch.setenv("BG_PAPER_AUTO_LOGIN", "1")
    monkeypatch.delenv("BG_PAPER_USERNAME", raising=False)
    monkeypatch.delenv("BG_PAPER_PASSWORD", raising=False)
    with pytest.raises(ConfigError) as ei:
        validate_runtime_config()
    assert "BG_PAPER_USERNAME" in str(ei.value) or "BG_PAPER_PASSWORD" in str(ei.value)


def test_validate_invalid_stack_kind_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "production")
    with pytest.raises(ConfigError):
        validate_runtime_config()


# ---- StackKind ist Literal-Type-Helper, exportiert ----


def test_stack_kind_type_literal() -> None:
    """Sanity: StackKind ist als Literal exportiert (nicht runtime-checkbar,
    aber Import muss klappen)."""
    assert StackKind is not None


# ---- bestehende quotes_source weiterhin OK ----


def test_quotes_source_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_QUOTES_SOURCE", raising=False)
    assert quotes_source() == "polling"


# ---- BG_TWS_READ_ONLY (gateway <-> tws-Container Schalter) ----


def test_tws_read_only_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_TWS_READ_ONLY", raising=False)
    assert tws_read_only() is True


def test_tws_read_only_yes_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_TWS_READ_ONLY", "yes")
    assert tws_read_only() is True


@pytest.mark.parametrize("raw", ["no", "NO", "No", "  no  "])
def test_tws_read_only_only_no_enables_write(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("BG_TWS_READ_ONLY", raw)
    assert tws_read_only() is False


@pytest.mark.parametrize(
    "raw", ["yes", "true", "1", "0", "false", "off", "", "garbage"]
)
def test_tws_read_only_everything_else_stays_true(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    # Nur exakt "no" aktiviert write - alles andere (auch false/0/off, die
    # IBC nicht versteht) bleibt read-only: sicher + konsistent mit dem
    # tws-Container.
    monkeypatch.setenv("BG_TWS_READ_ONLY", raw)
    assert tws_read_only() is True


def test_validate_runtime_config_live_write_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hard-Guard 5: Live darf nie write sein.
    monkeypatch.setenv("BG_STACK_KIND", "live")
    monkeypatch.setenv("BG_TWS_READ_ONLY", "no")
    monkeypatch.delenv("BG_PAPER_AUTO_LOGIN", raising=False)
    monkeypatch.delenv("BG_PAPER_USERNAME", raising=False)
    monkeypatch.delenv("BG_PAPER_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="BG_TWS_READ_ONLY"):
        validate_runtime_config()


def test_validate_runtime_config_live_read_only_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "live")
    monkeypatch.delenv("BG_TWS_READ_ONLY", raising=False)
    monkeypatch.delenv("BG_PAPER_AUTO_LOGIN", raising=False)
    monkeypatch.delenv("BG_PAPER_USERNAME", raising=False)
    monkeypatch.delenv("BG_PAPER_PASSWORD", raising=False)
    validate_runtime_config()


def test_validate_runtime_config_paper_write_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Paper + write ist erlaubt - der Guard greift nur fuer live.
    monkeypatch.setenv("BG_STACK_KIND", "paper")
    monkeypatch.setenv("BG_TWS_READ_ONLY", "no")
    monkeypatch.delenv("BG_PAPER_AUTO_LOGIN", raising=False)
    monkeypatch.delenv("BG_PAPER_USERNAME", raising=False)
    monkeypatch.delenv("BG_PAPER_PASSWORD", raising=False)
    validate_runtime_config()
