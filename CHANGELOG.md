# Changelog

Alle bemerkenswerten Aenderungen am Service. Format lose an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/) angelehnt;
SemVer in `pyproject.toml`.

## [1.6.1] — 2026-04-27

### Geaendert
- Portfolio-Adapter (`cp/portfolio.py`) auf die laut IBKR-Doku korrekten
  REST-Pfade umgestellt: `GET /portfolio/{accountId}/summary` (nativ
  statt aggregiert), `GET /portfolio/{accountId}/positions/{pageId}` mit
  Pagination (Default-Pagesize 30) und `GET /portfolio/{accountId}/ledger`.
  Vorher: nicht-existente `/iserver/account/{aid}/portfolio` und
  Singular-Varianten ohne `/portfolio`-Prefix - Live-Recording in
  AP-02 #04 lieferte HTTP 404. Erste von vier Sub-Karten in AP-02 #07.
- `normalize_summary_money` in `broker_gateway.money`: konvertiert das
  IBKR-Summary-Feld-Schema `{amount, currency, value, isNull, timestamp}`
  in `Money` und respektiert `isNull=True`.
- `tests/cp_mock/replay.py` und seed-Recordings (`tests/fixtures/recorded/seed/`)
  auf die neuen Pfade umgestellt; alte `iserver_account_U25235077_*`-Seeds
  geloescht. Throttle-Klassifizierung (`throttle/manager.py`) zieht mit.

### Hinweis
- v1-API-Vertrag unveraendert. Reine Adapter-Korrektur. Live-Recordings
  fuer die korrigierten Pfade existieren bereits aus AP-02 #04 (v1.3.0)
  und werden vom Replay-Loader vorrangig gegenueber den Seeds verwendet.

## [1.6.0] — 2026-04-26

### Hinzugefuegt
- Drift-Detection: `scripts/check_mock_drift.py` vergleicht
  Live-CP-Gateway-Antworten gegen `tests/fixtures/recorded/live/` und
  schreibt `reports/drift/<YYYY-MM-DD>.md` mit Klassifikation pro
  Endpunkt (no/minor/value/breaking). Exit 0 bei nur additivem/value
  drift, Exit 1 bei breaking drift, Exit 3 wenn `/iserver/auth/status`
  nicht authentifiziert ist.
- `tests/cp_mock/diff.py` als Single Source of Truth fuer Drift-Logik
  (`DiffReport`, `diff_recording`, `DEFAULT_IGNORE_FIELDS`). 28 Unit-
  Tests in `tests/test_drift_diff.py` decken alle Klassifikationsfaelle
  ab (added/removed/type-change/value-change/null-Edges/Listen-Diff).
- `scripts/recording_session.py refresh <fixture>`-Subkommando: zeigt
  Diff vorher und ersetzt eine einzelne Fixture nur nach expliziter
  Bestaetigung. CI-Modus mit `--yes`.
- 10 Tests in `tests/test_check_mock_drift.py` (MockTransport-basiert):
  no/additive/breaking/value-Drift, Status-Code-Aenderung, Skip-Logik
  fuer Order-Endpunkte und 4xx/5xx-Recordings, Markdown-Rendering.
- `docs/runbooks/mock-drift-check.md` mit Reaktion pro Drift-Klasse,
  Refresh-Workflow und Troubleshooting.
- `docs/cp-recordings.md` Section "Drift Detection" + "Refresh".
- Erster eingecheckter Drift-Bericht unter `reports/drift/2026-04-26.md`
  (9 no drift, 6 value drift, 0 breaking drift, 7 uebersprungen).

### Geaendert
- README-Status auf v1.6.0; AP-02 (Live-IBKR-Validierung) abgeschlossen.

### Behoben
- `diff_recording`: beidseitiges `None` wird nicht mehr als
  `value drift` mit Note `filled-in` gemeldet. Vorher hat jedes
  optionale, dauerhaft leere Feld bei jedem Live-Lauf einen
  Lauer-Eintrag erzeugt.

### Bekannt
- Order-Endpunkte (`/orders`, `/order/`) und Session-Wechsler (`/logout`,
  `/reauthenticate`) werden vom Drift-Check uebersprungen - Mock fuer
  Order bleibt seed/erste-Live-Aufzeichnung.
- Service-Code-Pfad-Bugs (cp/portfolio.py, cp/orders.py, cp/lifecycle.py)
  bleiben offen unter Folgekarte 813fed62 (siehe v1.3.0-Bekannt).

## [1.5.0] — 2026-04-25

### Hinzugefuegt
- Vereinheitlichtes Error-Modell `{error: {code, message, request_id,
  retry_after_s, extra}}` fuer **alle** v1-Endpunkte (Section 1.6 final).
  Single Source of Truth: `src/broker_gateway/api/v1/errors.py`.
  Globale Exception-Handler in `main.py` uebersetzen `HTTPException`,
  `RequestValidationError` und die neue `CPGatewayError` ins Schema.
- `scripts/recording_session.py error-path` provoziert IBKR-Fehler:
  Pacing-Violation, ungueltige conid, ungueltige Order-Quantity,
  nicht-existente Order-ID, optional Reauth-Fail (`--with-reauth-fail`).
- 7 Live-Error-Recordings unter `tests/fixtures/recorded/live/errors/`
  + Manifest. Wertvollster Fund: IBKR liefert generisches HTTP 500/503
  statt 4xx — Service-Code-Mapping muss aus dem Body-Inhalt schliessen.
  Bonus aus dem Reauth-Fail-Lauf: `/iserver/auth/status` bleibt nach
  `/logout` erreichbar mit `{authenticated: false, established: false,
  competing: false, connected: false, MAC: null}` — das ist das
  zuverlaessige Signal fuer `cp/lifecycle.py`, in `AUTH_LOST` zu kippen.
  `/reauthenticate` ohne Session liefert HTML 404 (kein JSON).
- `tests/test_error_model.py` mit 14 Tests (5 Pflicht-Cases plus
  Default-Code-Mapping-Parametrisierung).
- `docs/runbooks/recording-session-error-path.md` mit Reset-Anleitung
  nach Reauth-Fail und Diff-Bewertung des ersten Live-Laufs.

### Geaendert
- `cp/quotes.py::_call_snapshot` differenziert HTTP 429 jetzt explizit
  als `cp_pacing_violation` mit `Retry-After`-Header statt allgemeines
  `cp_upstream_error`.
- `cp/lifecycle.py::require_session_ok` setzt `code: "auth_lost"` und
  `retry_after_s: 30` im Detail.
- `auth/middleware.py` setzt explizit `missing_token`, `invalid_token`,
  `missing_scope` mit `required_scope` im `extra`.
- 3 Tests strukturell angepasst: `body["detail"]` -> `body["error"]["message"]`
  (test_auth, test_quotes_snapshot, test_events_stream) — kein Test-Intent
  geaendert, nur das Lese-Schema.

### Bekannt — fuer Folgekarte 813fed62
- IBKR liefert HTTP 500/503 fuer Anwendungs-Fehler. Der Service-Code
  sollte CP-Gateway-Bodies inspizieren und in semantische `code`-Werte
  uebersetzen (z.B. Body enthaelt "is not found" -> `not_found`,
  Body enthaelt "is not valid" -> `invalid_input`).
- IBKR-Pacing griff im ersten Live-Lauf nicht (60 Calls/s = alle 200 OK).
  Re-Test sobald IBKR-Wartung vorbei ist.

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
- `cp/lifecycle.py::reauthenticate` ohne `?force=true`. IBKR-Doku
  erlaubt bei `competing: true` ein Force-Reclaim — broker-gateway als
  dokumentierter Single-Owner sollte das nutzen koennen.
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
