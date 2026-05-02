"""Stabile conid-Konstanten fuer Paper-Tests (AP-08 K3).

Hardcoded weil die IBKR-conids fuer die Top-US-Werte ueber Jahre
stabil sind. Falls IBKR doch mal Konten- oder Index-Splits durchfuehrt,
wird hier ein einzelner Eintrag aktualisiert.
"""
from __future__ import annotations


# Quelle: docs/research/ibkr-cpapi-websockets-findings.md (AP-04 K4).
CONID_AAPL = 265598
CONID_MSFT = 272093
CONID_AMZN = 3691937
CONID_SAP = 13977
CONID_META = 107113386
CONID_GOOGL = 208813720


__all__ = [
    "CONID_AAPL",
    "CONID_AMZN",
    "CONID_GOOGL",
    "CONID_META",
    "CONID_MSFT",
    "CONID_SAP",
]
