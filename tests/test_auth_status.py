"""Tests fuer broker_gateway.auth_status (Karte 33cb35b1 Phase 2).

Pruefen, dass:

- Alle 6 Enum-Werte vorhanden sind und stabile string-Werte haben
  (Schema-Stabilitaet ueber Releases).
- ``to_consumer_status`` alle Werte deterministisch auf ``ok | down |
  lost`` mappt.
- ``is_session_unavailable`` den 503-Guard-Vertrag korrekt umsetzt
  (CP_DOWN/AUTH_LOST/TWS_DOWN/SESSION_LOST → True; OK/REAUTH_PENDING
  → False).
- Backward-Compat: ``broker_gateway.cp.lifecycle.AuthStatus`` ist
  identisch mit ``broker_gateway.auth_status.AuthStatus`` (Re-Export).
"""
from __future__ import annotations

import pytest

from broker_gateway.auth_status import (
    AuthStatus,
    is_session_unavailable,
    to_consumer_status,
)


class TestEnum:
    def test_all_six_values_present(self) -> None:
        assert {s.value for s in AuthStatus} == {
            "ok",
            "reauth_pending",
            "auth_lost",
            "cp_down",
            "tws_down",
            "session_lost",
        }

    def test_string_enum_serialises_to_value(self) -> None:
        assert AuthStatus.TWS_DOWN.value == "tws_down"
        assert str(AuthStatus.SESSION_LOST.value) == "session_lost"

    def test_backward_compat_import_from_cp_lifecycle(self) -> None:
        # cp/lifecycle.py importiert AuthStatus aus auth_status.py
        # und re-exportiert. Bestehender Code, der `from
        # broker_gateway.cp.lifecycle import AuthStatus` nutzt, muss
        # weiter funktionieren und denselben Enum sehen.
        from broker_gateway.cp.lifecycle import AuthStatus as ReExported

        assert ReExported is AuthStatus


class TestToConsumerStatus:
    @pytest.mark.parametrize(
        ("backend_status", "expected"),
        [
            (AuthStatus.OK, "ok"),
            (AuthStatus.REAUTH_PENDING, "lost"),
            (AuthStatus.AUTH_LOST, "lost"),
            (AuthStatus.SESSION_LOST, "lost"),
            (AuthStatus.CP_DOWN, "down"),
            (AuthStatus.TWS_DOWN, "down"),
        ],
    )
    def test_mapping(
        self, backend_status: AuthStatus, expected: str
    ) -> None:
        assert to_consumer_status(backend_status) == expected

    def test_mapping_covers_every_enum_value(self) -> None:
        # Defensive: wenn ein neuer Enum-Wert hinzukommt und das
        # Mapping nicht erweitert wird, schlaegt to_consumer_status
        # mit KeyError fehl. Der Test fixiert die Vollstaendigkeit.
        for status in AuthStatus:
            result = to_consumer_status(status)
            assert result in {"ok", "down", "lost"}


class TestIsSessionUnavailable:
    @pytest.mark.parametrize(
        "status",
        [
            AuthStatus.CP_DOWN,
            AuthStatus.AUTH_LOST,
            AuthStatus.TWS_DOWN,
            AuthStatus.SESSION_LOST,
        ],
    )
    def test_unavailable_states_return_true(self, status: AuthStatus) -> None:
        assert is_session_unavailable(status) is True

    @pytest.mark.parametrize(
        "status",
        [AuthStatus.OK, AuthStatus.REAUTH_PENDING],
    )
    def test_available_states_return_false(self, status: AuthStatus) -> None:
        # REAUTH_PENDING ist bewusst NICHT 503 - der Reauth-Loop
        # erholt sich typischerweise innerhalb von Sekunden.
        assert is_session_unavailable(status) is False
