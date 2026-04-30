# Changelog

Alle bemerkenswerten Aenderungen am Service. Format lose an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/) angelehnt;
SemVer in `pyproject.toml`.

## [1.11.0] — 2026-04-30

AP-04 K3 - WS-Client als wiederverwendbarer Baustein. ``CPWebSocketClient``
kapselt connect, Auth-Frame, Auth-Ack-Wait (sts.authenticated=true),
async-Frame-Iteration, send, tic-Ping-Loop und Reconnect mit exponential
backoff. Single-Owner-Konstraint: pro Instanz nur ein ``connect()``. Der
Baustein wird in dieser Karte NICHT in main.py oder einen Endpoint
eingebunden - Konsumenten (Quotes-Stream, EventBus, SSE-Mapping) entstehen
spaeter im AP-04 K6 / Folge-AP.

### Hinzugefuegt
- `src/broker_gateway/cp/ws_client.py`: Klasse ``CPWebSocketClient`` plus
  ``WSIncomingFrame``, ``WSAuthError`` und das ``WSConnection``-Protocol.
  Connect-Default ``ws://cpgateway:5000/v1/api/ws`` (Plain-HTTP wie der
  REST-Pfad im Compose-Netzwerk), Override per ENV ``BG_CP_WS_URL``.
  Cookie-Reuse aus dem REST-Client erfolgt explizit als
  Methodenparameter (kein Import-Coupling). TLS-Strategie liegt komplett
  bei der ``websockets``-Lib - kein lokaler SSL-Override.
- `src/broker_gateway/cp/__init__.py`: Re-Export der drei oeffentlichen
  Symbole + ``WSConnection``.
- `tests/test_ws_client.py`: 10 Tests gegen einen In-Memory-FakeConnection-
  Stub (kein echter Socket noetig). Deckt connect+auth, Auth-Failure
  (``sts.authenticated=false``), Auth-Timeout, tic-Ping-Loop, Reconnect
  bei broken pipe, Aufgabe nach max-Reconnect-Attempts, Single-Owner-
  Doppel-Connect-Reject, Frame-Iteration mit der spike-baseline-Fixture
  aus K2, send-vor-connect und send-nach-close.

### Geaendert
- `pyproject.toml`: neue Runtime-Dep ``websockets>=12``. Begruendung
  (Implementation-Log): pure-async, gut testbar via injizierbarer
  connect-Factory, kein C-Extension-Build noetig auf cma-pi-1.
- `compose.yaml`, `src/broker_gateway/__init__.py`,
  `tests/test_health.py`, README-Footer: 1.10.0 -> 1.11.0.

### Bekannte Einschraenkungen
- Der WS-Client ist nirgends im App-Lifespan instanziiert - das ist die
  bewusste Karten-Abgrenzung. Naechste Schritte: AP-04 K4 (Topic-
  Exploration), K5 (Consumer-Fragebogen), K6 (Architektur-Decision-Gate).
- ``tic``-Multiplikator (4 Server-Antworten pro Client-Ping, dokumentiert
  in `docs/research/ibkr-cpapi-websockets-findings.md`) wird vom Frame-
  Iterator unveraendert durchgereicht. Dedup ist Konsumenten-Logik (z.B.
  EventBus) und kommt in einer Folge-Karte.

## [1.10.0] — 2026-04-29

AP-05 Karte 2/3 - Inbound-Body-Logging. Die ObservabilityMiddleware
schreibt jetzt zusaetzlich zu den Metadaten `request_headers`,
`request_body`, `response_headers`, `response_body` und
`response_streaming` ins `http_request`-Event. Bodies werden 1:1
abgelegt (kein Normalize, keine Truncation); Header werden ueber
`broker_gateway.cp.redaction.filter_headers` gefiltert. SSE-Antworten
(`text/event-stream`) bleiben unangetastet und werden mit
`response_streaming=true` markiert. `request_id` wird via
`structlog.contextvars.bind_contextvars` an den ContextVar-Stack
gebunden - damit landet sie automatisch in jedem nachgelagerten Event
derselben Request-Verarbeitung (Vorbereitung fuer Karte 3 CP-Wire).

### Hinzugefuegt
- `BG_LOG_INBOUND_BODIES` (default `on`): Notfall-Schalter zur
  Deaktivierung der Body-/Header-Erfassung. `off`/`0`/`false`/`no`
  schaltet ab; Metadaten bleiben in beiden Modi unveraendert.
- Neue Test-Cases in `tests/test_observability.py`: response_body bei
  GET, request_body bei POST (`/v1/auth/token`), 422-Pfad mit Body,
  Authorization/Cookie/X-API-Key/X-Auth-Token nie im Log,
  `BG_LOG_INBOUND_BODIES=off`-Verhalten, Stream-Replacement (Endpunkt
  sieht den Body nach Middleware-Read), SSE-Endpunkt mit
  `response_streaming=true`.

### Geaendert
- `src/broker_gateway/middleware/observability.py`: Stream-Replacement
  fuer Request-Body via `request._receive`-Replay, Response-Body durch
  Materialisierung von `body_iterator` (neuer Response gebaut, damit
  der Iterator nicht zweimal konsumiert wird), Streaming-Erkennung via
  `Content-Type: text/event-stream`. Pre-Read nur fuer Requests mit
  `Content-Length > 0` oder `Transfer-Encoding: chunked` - sonst
  bleibt der ASGI-receive-Stream unberuehrt (sonst kollidiert das
  Replay mit `BaseHTTPMiddleware.wrapped_receive` bei
  GET-/SSE-Endpunkten).
- `compose.yaml`, `pyproject.toml`, `__init__.py`, `test_health.py`,
  README-Footer: 1.9.0 -> 1.10.0.
- README Observability-Section: Body-/Header-/Streaming-Felder, neue
  ENV-Variable `BG_LOG_INBOUND_BODIES`.

### Bekannte Einschraenkungen
- `cp_wire.log` bleibt leer, bis Karte 3 (CP-Wire-Log) den
  `broker_gateway.cp.wire`-Logger befuellt. Die Korrelation per
  `request_id` ist bereits vorbereitet.
- Bodies werden ohne Truncation geschrieben - bei einzelnen sehr
  grossen Payloads (Bulk-Order, grosse Quotes-Snapshots) kann
  `inbound.log` schnell wachsen. Rotation via
  `BG_LOG_INBOUND_MAX_BYTES`/`BG_LOG_INBOUND_BACKUP_COUNT` greift
  trotzdem; pro-Event-Truncation ist out-of-scope dieser Karte.

## [1.9.0] — 2026-04-29

AP-05 Karte 1/3 - Logging-Backbone. structlog/stdlib-Pipeline auf einen
gemeinsamen JSONRenderer harmonisiert; Routing per Logger-Name auf drei
Straenge (`broker_gateway.http` -> `inbound.log`, `broker_gateway.cp.wire`
-> `cp_wire.log`, `broker_gateway` -> `app.log`) wenn `BG_LOG_DIR`
gesetzt ist. Backwards-kompatibel - ohne `BG_LOG_DIR` schreiben alle
drei Logger weiter auf stdout. Die Inhalte der Logs aendern sich noch
nicht; Bodies kommen mit Karte 2 (Inbound) und 3 (CP-Wire).

### Hinzugefuegt
- `src/broker_gateway/cp/redaction.py`: `REDACTED_HEADERS` (frozenset,
  lower-case) und `filter_headers()` als Single Source of Truth fuer
  Header-Redaktion. Wird vom CPRecorder bereits genutzt; CP-Wire-Logger
  und Inbound-Body-Middleware werden ebenfalls darauf importieren.
- `src/broker_gateway/logging_setup.reset_for_testing()`: setzt das
  `_CONFIGURED`-Flag und structlog-Defaults zurueck, damit Tests mit
  geaenderten ENV-Variablen arbeiten koennen.
- ENV-Variablen `BG_LOG_DIR`, `BG_LOG_LEVEL`, `BG_LOG_ROTATE_MAX_BYTES`,
  `BG_LOG_ROTATE_BACKUP_COUNT` sowie pro-Strang-Overrides
  `BG_LOG_INBOUND_*`, `BG_LOG_CP_WIRE_*`, `BG_LOG_APP_*`.
- `tests/test_cp_redaction.py`, `tests/test_logging_setup.py`.

### Geaendert
- `src/broker_gateway/logging_setup.py`: structlog nutzt jetzt
  `structlog.stdlib.LoggerFactory()` statt `PrintLoggerFactory`, und
  formatiert via `structlog.stdlib.ProcessorFormatter` mit
  `foreign_pre_chain`. Damit laufen stdlib-Logger (Throttle, Streams,
  CP-Lifecycle, Recorder) durch denselben JSONRenderer wie
  Bound-Logger - die README-Aussage "jede Log-Zeile ist ein JSON-Dict"
  ist jetzt tatsaechlich wahr. stdout-Default-Pfad nutzt `_LazyStdout`,
  damit pytest-`capsys` die Reference auch nach Modul-Import noch
  patchen kann.
- `src/broker_gateway/cp/recorder.py`: importiert `REDACTED_HEADERS`
  und `filter_headers` aus `cp/redaction.py`; lokale Kopie entfernt.
- `compose.yaml`, `src/broker_gateway/__init__.py`,
  `tests/test_health.py`, README-Footer: 1.8.0 -> 1.9.0.

### Bekannte Einschraenkungen
- Inhalte der Logs sind noch unveraendert (Bodies fehlen weiter in
  `inbound.log`, der CP-Wire-Strang ist noch leer). Das ist Scope von
  Karten 2 und 3 in AP-05.

## [1.8.0] — 2026-04-28

Release-Karte AP-03 - duale Drift-Detection. Doku-Drift-Check als
Frueh-Warner (taeglich, ohne Auth) plus Live-Drift als Build-Acceptance-
Test. Karte forderte 1.7.0; tatsaechlich 1.8.0, weil 1.7.0 schon durch
AP-02 #07-4 belegt war.

### Hinzugefuegt
- `tests/cp_doc/diff.py`: `diff_openapi(actual, expected) -> SpecDiffReport`.
  Klassifiziert OpenAPI-/Swagger-Spec-Aenderungen in vier Stufen:
  `no drift`, `minor (additive)`, `value (irrelevant)`, `breaking`. Behandelt
  Pfad-/Operation-/Status-Code-/Schema-/Enum-/Required-Aenderungen,
  unterscheidet Request- und Response-Mode (z.B. neues required Request-
  Feld = breaking, neues Response-Feld = minor).
- `scripts/check_doc_drift.py`: CLI-Skript, das die Live-IBKR-OpenAPI-Spec
  laedt und gegen `docs/research/ibkr-cpapi-doc.json` vergleicht. Schreibt
  Bericht nach `reports/doc-drift/<YYYY-MM-DD>.md`. Exit-Codes 0/1/2/3
  (no/breaking/minor/unreachable). Mit `--auto-card` legt es eine
  KanPrompt-Karte via REST an; Spam-Schutz: max. 1 Karte pro Tag pro
  Drift-Klasse, Praefix-Check via `GET /api/v1/projects/.../cards`.
- `ops/systemd/doc-drift.{service,timer}` plus `doc-drift.env.example` und
  `ops/systemd/README.md`: taeglicher Lauf um 06:00 Europe/Berlin auf
  cma-pi-1. KANPROMPT_API_KEY kommt aus `/etc/default/doc-drift`, niemals
  aus dem Repo.
- `ops/build-gateway.sh`: Build-Wrapper `docker compose build` ->
  `check_mock_drift --build-acceptance` -> `docker compose up -d`. Bricht
  ab, wenn der Drift-Check fehlschlaegt. `SKIP_ACCEPTANCE=1` als Notfall-
  Bypass.
- `scripts/check_mock_drift.py`: neuer `--build-acceptance`-Modus mit 90s
  Warmup-Pause vor dem ersten Replay (project_ibkr_session_resume),
  strengerem Exit-Code (auch value drift = exit 1) und Berichts-Pfad
  `reports/drift/build-<commit-sha>.md`. Ohne den Flag: bestehendes
  Verhalten unveraendert.
- `tests/test_doc_drift.py`: 20 Unit-Tests fuer `diff_openapi` (alle 11
  Pflichtfaelle aus der Karte plus defensive Zusatzfaelle wie
  required-Aenderungen in Response, Enum-Removal in Request, gemischte
  Severities, Markdown-Render).
- `tests/test_check_doc_drift.py`: 11 Integrationstests mit
  `httpx.MockTransport` (Exit-Codes, Berichts-Datei, Auto-Karten-Anlage
  mit Spam-Schutz, Fehlerpfade).
- `docs/runbooks/doc-drift-check.md`: vollstaendiges Runbook fuer den
  Doku-Drift-Check inkl. Drift-Strategie-Schaubild, Reaktion pro
  Klassifikation, Spam-Schutz-Erklaerung, Baseline-Update-Workflow,
  Troubleshooting.
- `reports/doc-drift/2026-04-28.md`: erster Doku-Drift-Bericht (analog
  zur Karte AP-02 #06).

### Geaendert
- `docs/runbooks/mock-drift-check.md`: Section "Build-Acceptance-Modus"
  ergaenzt; "Wann laufen wir das" auf "bei jedem Container-Rebuild + ad
  hoc" angepasst (woechentliche Routine entfaellt).
- `docs/cp-recordings.md`: Section "Drift-Strategie" voran gestellt mit
  Schaubild Doku-Drift (Frueh-Warner) vs. Live-Drift (Build-Acceptance).

### Version-Bump
- `pyproject.toml`, `src/broker_gateway/__init__.py`, `compose.yaml`
  Image-Tag, `tests/test_health.py`, README-Footer: 1.7.0 -> 1.8.0.

## [1.7.0] — 2026-04-28

Release-Karte AP-02 #07-4 - Live-Recording-Lauf gegen die in
1.6.1/1.6.2/1.6.3 korrigierten Service-Pfade. Aggregiert die vier
AP-02 #07-Sub-Karten in einen Minor-Release. Karte sprach urspruenglich
von 1.4.0; tatsaechliche Versionsnummer ist 1.7.0, weil 1.6.x in den
Sub-Karten 1-3 verbraucht wurde.

### Hinzugefuegt
- `scripts/recording_session.py happy-path` zeichnet zusaetzlich
  `GET /sso/validate` (Schritt `a++)`) auf - Replay-Mock kann jetzt auf
  reale Bodies fuer den primaeren Keep-Alive zurueckgreifen.
- `src/broker_gateway/cp/normalize.py`: neues `_SECRET_FIELDS_LOWER`
  redacts `TOKEN`, `CREDENTIAL`, `IP`, `USER_NAME`, `USER_ID`,
  `UNIQUE_LOGIN_ID`, `MAC`, `hardware_info`, `userId` (tickle) und
  weitere sensible Felder aus `sso/validate`/`auth/status`/`tickle` auf
  `<REDACTED>`. Erstes 07-4-Recording hatte einen Auth-Token im
  `sso/validate`-Body geleakt; geleakte Files wurden nicht commited
  und durch redacted Live-Recordings ersetzt.

### Geaendert
- 23 Live-Recordings unter `tests/fixtures/recorded/live/` neu
  aufgezeichnet (broker-gateway 1.6.3, IBKR Build 10.45.1a). Alle
  v1-Service-Pfade liefern HTTP 200; die 7 dokumentarischen 404er sind
  alte Probe-Calls (Service-Code ruft sie nicht mehr) plus
  unsubscribe-Live-Artefakt.
- 5 synthetische Seeds aus AP-02 #07-1/3 entfernt
  (`portfolio/{summary,positions/0,ledger}`, `iserver/accounts`,
  `sso/validate`); alle haben nun reale Live-Pendants.
- `docs/runbooks/recording-session-happy-path.md`: Diff-Report
  2026-04-28 ergaenzt mit Verification-Tabelle aller korrigierten
  Pfade, geloeschte Seeds, Recorder-Filter-Erweiterung und
  IBKR-Server-Build-Drift `JifZ28031/10.44.1h -> JifZ20074/10.45.1a`.

### Hinweis
- Drift-Detection-Smoke-Test (zweiter Lauf, warm): 0 breaking, 1 minor
  (`sso/validate.isGw` additive), 4 value (Timestamps + FX-Bruchteile -
  normale Live-Schwankung). Erster Lauf zeigte 1 breaking
  (`marketdata/snapshot.6509: DPB -> ZB`), das war Cold-Session-Effekt
  - im zweiten Lauf bestaetigt sich das nicht. Verhalten ist im
  Auto-Memory `project_ibkr_session_resume` dokumentiert.
- v1-API-Vertrag unveraendert. Schliesst AP-02 Karte 07
  (Service-Code-an-reale-IBKR-Pfade) ab.

## [1.6.3] — 2026-04-28

### Hinzugefuegt
- `CPGatewayClient.sso_validate()` und `CPGatewayClient.iserver_accounts()`
  als neue Lifecycle-Endpunkte (GET /sso/validate, GET /iserver/accounts).
- `AuthLifecycle._maybe_init_accounts()` ruft `GET /iserver/accounts`
  beim ersten erfolgreichen Tickle nach Login auf und persistiert das
  Ergebnis als `accounts_initialized=True`. IBKR setzt diesen Call vor
  dem ersten Order- oder Portfolio-Aufruf voraus.
- `AuthLifecycle._heartbeat_sso()`: primaerer Keep-Alive geht jetzt
  ueber `GET /sso/validate` (Spec-Empfehlung). Tickle bleibt als
  sekundaerer CP-Health-Indicator und Backward-Compat-Pfad erhalten -
  ein Tickle-Fehler bei gueltigem sso/validate landet nicht mehr in
  CP_DOWN.
- `AuthLifecycle.reauthenticate(force=False)`: oeffentliche Methode
  fuer manuelle Reauth-Triggers. Mit `force=True` wird
  `POST /iserver/reauthenticate` unconditional ausgeloest und der
  Auth-Status danach geprueft - hilft im cold-tunnel-Fall (siehe
  Auto-Memory `project_ibkr_session_resume`), in dem `auth/status`
  faelschlich `authenticated=false` meldet, der Reauth aber sofort
  durchgeht.
- `LifecycleSnapshot` und `/v1/internal/health` exponieren neu
  `last_sso_validate_at`, `last_login_at`, `accounts_initialized`.

### Geaendert
- `tests/cp_mock/replay.py`: neue Mock-Routen fuer GET /sso/validate
  und GET /iserver/accounts. Bei `auth_lost=True` liefert sso/validate
  `RESULT=false`. Seed-Recordings fuer beide Endpunkte unter
  `tests/fixtures/recorded/seed/`; Live-Recording fuer
  `iserver/accounts` existiert bereits aus AP-02 #04.

### Hinweis
- v1-API-Vertrag unveraendert. `force` ist interner Lifecycle-Schalter,
  kein Vertragsfeld. Dritte von vier Sub-Karten in AP-02 #07.

## [1.6.2] — 2026-04-28

### Geaendert
- `cp/orders.py::OrdersService.get_order` ruft jetzt den IBKR-Singular-
  Pfad `GET /iserver/account/order/status/{orderId}` (vorher: nicht
  existenter Bulk-Pfad `/iserver/account/orders/{orderId}`). Quelle:
  `docs/research/ibkr-cpapi-doc.json`.
- `cp/trades.py::_map_trade` mappt das IBKR-Live-Feld `account` (sowie
  `accountCode`) auf das v1-Vertragsfeld `account_id` und leitet die
  Currency aus `listing_exchange` ab. Eine kleine Tabelle deckt die fuer
  U25235077 relevanten Boersen ab (NYSE/NASDAQ/ARCA -> USD,
  IBIS/FWB/AEB -> EUR, LSE -> GBP usw.); ohne Match faellt der Adapter
  auf den USD-Default zurueck und setzt `currency_assumed=True`.
  Ein explizit gesetztes `currency`-Feld (FX-Cash-Trades) schlaegt den
  Exchange-Lookup weiterhin.
- `availability.py`: Prefix `Z` (Frozen) und `Y` (Frozen Delayed) laut
  IBKR-OpenAPI-Spec ergaenzt - `ZB` taucht in realen Marketdata-
  Antworten auf und wurde bisher als unbekannt gemeldet.
- `tests/cp_mock/replay.py`: Mock-Order-Status-Pfad und Mock-Trade-Body
  an das IBKR-Live-Schema angeglichen (Singular-Pfad, `account` +
  `listing_exchange` statt `account_id` + `currency`). Flag
  `omit_trade_currency` entfernt nun zusaetzlich `listing_exchange`,
  damit der Fallback-Pfad weiterhin getestet wird.

### Hinweis
- v1-API-Vertrag unveraendert. Reine Adapter-Schicht. Zweite von vier
  Sub-Karten in AP-02 #07. Live-Recording der korrigierten Pfade folgt
  in der vierten Sub-Karte.

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
