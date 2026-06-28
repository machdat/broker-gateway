# tests_paper/L2_safe_write/

L2 — Sichere Schreib-Operationen ohne Order-Lifecycle. Marker:
`paper_safe_write`. Beispiele: Token-Erstellung im Paper-Auth-Store,
Cache-Reset-Aufrufe.

## Aufruf

```bash
BG_PAPER_BASE_URL=http://cma-pi-1:4001 \
BG_PAPER_BOOTSTRAP_TOKEN=<admin-token> \
BG_PAPER_ACCOUNT_ID=<paper-du-konto> \
pytest -m paper_safe_write tests_paper/L2_safe_write/
```

Voraussetzungen: Paper-Stack läuft auf cma-pi-1:4001 (siehe
`docs/runbooks/paper-account-setup.md`). Bootstrap-Token kommt aus
`.env.paper` (`BG_BOOTSTRAP_ADMIN_TOKEN`).

## Tests

| Datei | Karte | Inhalt |
|-------|-------|--------|
| `test_token_lifecycle.py` | AP-12 L2-1 | POST /v1/auth/token, Self-Revoke, 403 bei non-admin Create/Revoke |
| `test_status_endpoint.py` | AP-12 L2-2 | GET /v1/status liefert v1-Envelope, ohne Bearer 401 |
| `test_sse_refcount.py` | AP-12 L2-2 | Zwei parallele SSE-Consumer bekommen Frames; Stream nach Cool-Down erneut nutzbar |

> Hinweis Refcount: `subscriptions_active` im Status-Endpoint zählt nur
> WS-Push-Subs (Registry, ab AP-11 K9). Im polling-Default ist der Wert
> auch bei aktivem Stream `0`. Der SSE-Behavior-Test (`test_sse_refcount.py`)
> verifiziert daher direkt am Stream, nicht am Status-Counter.
