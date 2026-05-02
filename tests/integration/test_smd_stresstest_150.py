"""150-Symbol-smd-Stresstest gegen U25235077 (AP-11 K8).

Skelett-Test, der NICHT in der Standard-Suite laeuft (Marker
``integration`` und ``live`` sind in pytest.ini default deselected).
Aufruf:

    pytest -m "live and stresstest" \
        tests/integration/test_smd_stresstest_150.py

Voraussetzung: AP-11 K3-Folgekarte ist fertig (CPWebSocketClient im
Lifespan instanziiert), `BG_QUOTES_SOURCE=ws` ist gesetzt, und das
CP-Gateway hat eine Live-Session zu U25235077.

Erwartungswerte (aus K6-Sektion 4.4):

- p50 < 100 ms (CP-Receive bis Egress).
- p95 < 250 ms (Sicherheitspuffer ueber dem Robot-SLO von 150 ms).
- Drop-Counter pro Topic-Adapter unter 1 % der Frames.

Die Implementierung ist in dieser Karte als Stub vorgesehen - die
echten Latenz-Messungen koennen erst nach Lifespan-Wiring gegen das
Live-Konto laufen.
"""
from __future__ import annotations

import os

import pytest


pytestmark = [pytest.mark.live, pytest.mark.stresstest]


@pytest.mark.skipif(
    not os.environ.get("BG_LIVE_STRESSTEST"),
    reason="BG_LIVE_STRESSTEST nicht gesetzt - Live-Stresstest deselected",
)
def test_smd_150_symbols_p95_under_250ms() -> None:
    """Skelett: erwartet einen erfolgreichen Run gegen U25235077 mit
    150 Symbolen. Implementierung wird nach Lifespan-Wiring (AP-11 K3-
    Folgekarte) konkretisiert."""
    pytest.skip(
        "Implementierung folgt nach AP-11 K3-Folgekarte "
        "(Lifespan-Wiring fuer CPWebSocketClient + WSPushSource)."
    )
