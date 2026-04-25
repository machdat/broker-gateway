# Changelog

Alle bemerkenswerten Aenderungen am Service. Format lose an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/) angelehnt;
SemVer in `pyproject.toml`.

## [1.3.0] — 2026-04-25

### Hinzugefuegt
- Live-Recording-Session gegen das Konto **U25235077**: 22 JSON-Fixtures
  unter `tests/fixtures/recorded/live/` mit Manifest. `scripts/recording_session.py
  happy-path` ist voll implementiert (siehe
  `docs/runbooks/recording-session-happy-path.md`).
- IBKR Client Portal Web API Swagger-Snapshot in
  `docs/research/ibkr-cpapi-doc.json` als Quelle der Wahrheit fuer
  Endpunkt-Pfade.
- Diff-Report seed vs. live mit konkreten Funden im Runbook.

### Geaendert
- `tests/cp_mock/loader.py`: live-Recordings mit HTTP 4xx/5xx fallen auf
  seed zurueck — schuetzt Tests vor dokumentarischen Beweis-Recordings,
  ohne den Single-Source-of-Truth-Anspruch fuer happy-path-Bodies aufzugeben.
- `src/broker_gateway/cp/instruments.py`: `_map_search_entry` liest
  `sections[0].secType` als Fallback (Live-Schema), `_map_info` nimmt
  `ticker`/`listingExchange` als Fallbacks, `search()` filtert auf das
  primaere STK-Listing (IBKR liefert pro Symbol mehrere Listings).
- 3 Tests strukturell gelockert (tickle session, replay-loader MAC,
  instruments exchange) — akzeptieren jetzt sowohl seed-konkreten
  als auch live-normalisierten Wert.

### Bekannt — fuer Folgekarte AP-02 #X
- `cp/portfolio.py` nutzt **falsche Pfade**, die in der IBKR-Doku gar nicht
  existieren: `/iserver/account/{acct}/{portfolio,positions,ledger}`.
  Korrekt waere `/portfolio/{acct}/{summary,positions/{pageId},ledger}`.
- `cp/orders.py:95` Order-Status-Pfad `/iserver/account/orders/{id}` ist
  ein Bulk-Endpoint, korrekt waere `/iserver/account/order/status/{id}`.
- `cp/lifecycle.py` ruft `/iserver/accounts` nicht auf — IBKR antwortet
  ohne diesen Init mit 404 auf account-spezifische Endpunkte.
- `cp/lifecycle.py` Keep-Alive ueber `/tickle` — IBKR-Doku empfiehlt
  explizit `GET /sso/validate` jede Minute. Plus: 24h-Hard-Limit fuer
  Re-Auth, das vom Service nicht signalisiert wird.
- Snapshot-Prime-Verhalten: bei Polling kommen Werte sofort, kein Prime.

## [1.2.0] — 2026-04-25
- Mock-Fixture liest seed-Recordings ueber Replay-Loader. ReplayCPGatewayMock
  ersetzt MockCPGateway. tests/cp_mock-Modul mit Loader (live > seed).

## [1.1.0] — 2026-04-25
- CPRecorder + normalize_response fuer Live-Recordings. ENV
  `BG_CP_RECORD_DIR` aktiviert den Recorder; Header-Filter
  (Authorization/Cookie/Set-Cookie/X-API-Key); ID-/Timestamp-Sanitisierung
  in Bodies.

## [1.0.x] — 2026-04-23 bis 2026-04-25
- 1.0.4 Doku-Patch.
- 1.0.3 cpgateway-Container laeuft als non-root mit Host-User-Mapping.
- 1.0.2 CP-Gateway-Default-Base-URL um `/v1/api`-Prefix erweitert.
- 1.0.1 CP-Gateway-Container scharfgeschaltet inkl. Browser-2FA-Login-Runbook.
- 1.0.0 Erste vollstaendige Release: Observability (structured JSON-Logs +
  Prometheus `/metrics`).

## [0.x] — Foundation
- 0.12.0 Rate-Limit-Throttle.
- 0.11.0 Events-Stream (SSE).
- 0.10.0 Trades-History + MTD-Commission-Aggregat.
- 0.9.0 Order-Lifecycle mit Idempotency-Key + Reply-Confirmation-Loop.
- 0.8.0 Portfolio-Endpunkte mit Money-Normalisierung.
- 0.7.0 SSE-Quotes-Stream mit Refcount + Fan-Out.
- 0.6.0 Quotes-Snapshot mit First-Call-Prime + Availability-Normalisierung.
- 0.5.0 Instruments-Lookup mit Symbol-Cache.
- 0.4.0 CP-Gateway-Auth-Lifecycle inkl. `/v1/internal/health`.
- 0.3.0 Auth-Modell mit Token-Management.
- 0.2.0 pytest-Mock-Fixture.
- 0.1.0 `/v1/health`.
