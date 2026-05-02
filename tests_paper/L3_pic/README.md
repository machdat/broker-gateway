# tests_paper/L3_pic/

L3 — **Place + Immediate Cancel**: Limit-Order weit weg vom Markt
platzieren, sofort canceln. Marker: `paper_pic`. Keine Position
am Ende des Tests.

## Aufruf

```bash
BG_PAPER_BASE_URL=http://cma-pi-1:4001 \
BG_PAPER_BOOTSTRAP_TOKEN=<admin-token> \
BG_PAPER_ACCOUNT_ID=DUP799747 \
pytest -m paper_pic tests_paper/L3_pic/
```

Voraussetzungen: Paper-Stack läuft auf cma-pi-1:4001, Konto-ID
beginnt mit `DU` (Whitelist im DSL-Safety-Layer).

## Tests

| Datei | Karte | Inhalt |
|-------|-------|--------|
| `test_place_and_cancel.py` | AP-12 L3-1 | BUY-LMT 20% unter Markt, Submit-Status, Cancel, Cancel-Status; 404 bei unbekannter order_id |
| `test_idempotency_replay.py` | AP-12 L3-1 | Zweimal POST + DELETE mit gleichem Idempotency-Key liefert bitidentischen Body |

## Safety-Garantien

Im Cleanup-`finally` jedes Tests wird `cancel_all_open_orders` gerufen —
auch wenn der Test mid-flight aussteigt, bleibt keine offene Order
zurück. Limits aus `tests_paper/_dsl/safety.py`:

- DU-Whitelist (`BG_PAPER_ACCOUNT_ID` muss mit `DU` beginnen).
- `max_notional_per_order` Default 500 USD.
- Kill-Switch via `BG_PAPER_TESTS_DISABLED=true`.
