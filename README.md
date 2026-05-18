# broker-gateway

[![ci](https://github.com/machdat/broker-gateway/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/machdat/broker-gateway/actions/workflows/ci.yml)

Versionierte HTTP-API zwischen Consumern (PSM, trading-robot, ad-hoc CLI/Notebooks) und broker-vermittelten Diensten — Aktienhandel und Marktdaten-Streaming. Aktuell adaptiert ausschließlich **Interactive Brokers** über das Client Portal Gateway als interne Sub-Komponente. Das ist Absicht und kein Marketing-Versprechen für später: der Service entkoppelt Consumer von IBKR-Spezifika, damit das Adapter-Backend austauschbar bleibt, ohne dass `/v1` brechen muss.

**Status:** **v2.1.3 — HTTP-API-Cutover-Hold-out abgeschlossen + Portfolio-Resubscribe-Hang gefixt** (AP `2a203c58`, 2026-05-10). Alle Daten-Adapter sind im `BG_BACKEND=tws`-Pfad nativ über `ib_async` gegen IB Gateway implementiert: Portfolio (Phase 1), Instruments (Phase 2), Quotes (Phase 3), Orders + Trades (Phase 4), Calendar (Phase 5). Phase 6 hat den letzten cp-Hold-out abgesichert (`POST /v1/internal/seed-cookies` antwortet im tws-Mode mit HTTP 503 + `not_applicable_in_tws_mode`; `app.state.backend` als Single-Source-of-Truth, `app.state.cp_client` als Hard-Guard nur im cp-Mode). Phase 7 (v2.1.0) schliesst das AP mit Schema-Compat-Tests, Doku-Sweep und gebündeltem Pi-Deploy; v2.1.1–v2.1.3 räumen den nach der Phase-7-Live-Smoke entdeckten Resubscribe-Hang in `tws/portfolio.py` aus. Endstand v2.1.3: Subscribe-Cache + `asyncio.wait_for(ib.reqAccountUpdatesAsync(...), timeout=2.0s)` + synchroner Cache-Read aus `ib.portfolio()` / `ib.accountValues(...)` (Memory `project_tws_portfolio_resubscribe_hang`). broker-gateway läuft auf cma-pi-1 als TWS-only-Service: Live (U25235077, `:4000`) und Paper (DUP799747, `:4001`) sprechen `gnzsnz/ib-gateway:stable` mit IBC + Xvfb über die TWS-Socket-API. cpgateway-Service bleibt unter Compose-Profile `cp-legacy` als Notfall-Roll-Back-Material; vollständige Entfernung als Folgekarte nach 30 Tagen Stabilitäts-Beobachtung. Konsumenten-Vertrag (`/v1`) ist unverändert (Pydantic-Modelle werden zwischen `cp/`- und `tws/`-Adaptern geteilt, siehe `tests/test_tws/test_schema_compat.py`); `/v1/internal/health` rendert das stabile `auth_status_consumer`-Feld (`ok | down | lost`). Frühere Highlights: v2.0.0 Hard-Cutover (Karte 5), TWS-Refactor v1.32–v1.34 (368ccdfe Spike, 8b1781d3 Container-Slot, 441b53db Adapter, 33cb35b1 Lifecycle), Cookie-Bridge in v1.31.0. Aktueller Architektur-Stand in [`docs/02-architecture.md`](docs/02-architecture.md), Deploy-Workflow in [`docs/03-deployment.md`](docs/03-deployment.md), Versionshistorie in [`CHANGELOG.md`](CHANGELOG.md).

## Architektur und Doku

| Frage | Lebt in |
|---|---|
| Wie ist der Service intern gebaut? | [`docs/02-architecture.md`](docs/02-architecture.md) |
| Wie nutze ich die API als Consumer (kuratierter Einstieg)? | [`docs/05-api.md`](docs/05-api.md) |
| Welche Endpunkte gibt es, mit welchen Bodies/Headern (formale Spec)? | [`docs/api/v1.md`](docs/api/v1.md) |
| Wie deploye ich (Workflow, Pfade, Restart-Disziplin)? | [`docs/03-deployment.md`](docs/03-deployment.md) |
| Wie ist Security geregelt (Token, Scopes, Redaction, 2FA, Vorfall)? | [`docs/04-security.md`](docs/04-security.md) |
| Wie logge ich den CP-Gateway initial ein? | [`docs/runbooks/cpgateway-login.md`](docs/runbooks/cpgateway-login.md) |
| Welche IBKR-CP-API-Details liegen hinter Feld X? | [`docs/research/`](docs/research/) |
| Was bedeutet conid / Availability-Code / Refcount / Cassette? | [`docs/06-glossary.md`](docs/06-glossary.md) |
| Was war beim Bootstrap entschieden? | [`docs/01-context-from-bootstrap-session.md`](docs/01-context-from-bootstrap-session.md) |

Erste Anlaufstelle für neue Sessions ist `docs/02-architecture.md`. Alles weitere unten ist Quickstart und operationelle Referenz.

## Lokal starten

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows / Git-Bash: source .venv/Scripts/activate
pip install -e .[dev]
pre-commit install        # einmalig pro Clone — aktiviert den Recording-Token-Scan-Hook
pytest
uvicorn broker_gateway.main:app --reload
curl http://localhost:8000/v1/health
```

### Live-/Paper-Stack (cma-pi-1)

Beide Stacks teilen sich `compose.yaml` und unterscheiden sich nur in
Project-Name, Port, Volume und ENV-Datei (siehe AP-06 K2 +
[`docs/runbooks/paper-account-setup.md`](docs/runbooks/paper-account-setup.md)).
Default-Backend ist seit v2.0.0 **`tws`** (gnzsnz/ib-gateway:stable +
IBC + ib_async). Der cpgateway-Pfad bleibt als Notfall-Roll-Back-Profil
verfügbar.

```bash
# Live (Default-Stack auf TWS)
cp .env.live.template .env       # einmalig, Token + BG_TWS_USERNAME/PASSWORD
./ops/build-gateway.sh           # Port 4000, Project broker-gateway

# Paper (parallel zum Live-Stack)
cp .env.paper.template .env.paper  # einmalig, Token + DU-Konto + BG_TWS_*
./ops/build-gateway.sh --env=paper # Port 4001, Project broker-gateway-paper

# Cutover/Roll-Back-Skripte (kompakte Wrapper)
./ops/cutover-tws.sh   --env={live,paper}   # auf TWS-Backend (Default-Pfad)
./ops/rollback-to-cp.sh --env={live,paper}  # zurueck auf cpgateway (Notfall)
```

`.env`, `.env.paper` und `.env.live` sind in `.gitignore`. Templates
(`.env.example`, `.env.live.template`, `.env.paper.template`) werden
committed.

**Live-2FA-Lifecycle (chmangold/U25235077):** Bei jedem
Container-Recreate triggert IBC den IBKR-Login. Der `Second Factor
Authentication`-Dialog erscheint, der Operator muss via VNC die
Methode "IB" auswählen und am Handy zweimal die Push-Bestätigung
geben. Details + VNC-Tunnel-Anleitung in der Auto-Memory
`project_live_2fa_gnzsnz_pattern`. Paper (cborlm399) hat kein 2FA
und läuft skriptbar durch.

Der Pre-Commit-Hook läuft automatisch bei jedem `git commit` und scannt staged JSON/JSONL unter `tests/fixtures/recorded/` auf Authorization-Header, URL-safe-Token-Strings (≥ 32 Zeichen) und Cookie-Pattern. Single Source of Truth für die Header-Liste ist `broker_gateway.cp.redaction.REDACTED_HEADERS`. Manueller Lauf über alle Recordings: `pre-commit run --all-files`.

## Authentifizierung

Alle Endpunkte außer `GET /v1/health` verlangen einen Bearer-Token.
Tokens sind opake Strings (kein JWT) und werden serverseitig generiert.

Bootstrap: beim Start liest die App `BG_BOOTSTRAP_ADMIN_TOKEN` aus dem
Environment. Ist die Variable gesetzt, wird der Wert als Admin-Token mit
Scope `admin:*` registriert — initialer Einstiegspunkt für die
Token-Verwaltung. Niemals einen Bootstrap-Token-Wert ins Repo oder
Compose-File einchecken.

```bash
export BG_BOOTSTRAP_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn broker_gateway.main:app --reload

# 1) Consumer-Token mit gewünschten Scopes erzeugen
curl -X POST http://localhost:8000/v1/auth/token \
     -H "Authorization: Bearer $BG_BOOTSTRAP_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"caller_id":"psm","scopes":["quotes:read","portfolio:read","instruments:read"]}'

# 2) Mit dem ausgegebenen value gegen geschützte Endpunkte sprechen
curl -H "Authorization: Bearer <value-aus-Schritt-1>" http://localhost:8000/v1/...

# 3) Token revoken (Self oder admin:*)
curl -X DELETE -H "Authorization: Bearer <value>" http://localhost:8000/v1/auth/token
```

Persistenz wahlweise über `BG_TOKEN_FILE=/var/lib/broker-gateway/tokens.json`
(JSON-Backend, atomare Writes). Ohne diese Variable arbeitet der Service
mit einem In-Memory-Store — Tokens gehen beim Neustart verloren, was zur
transienten Service-Natur passt.

## Auth-Lifecycle (TWS-Backend; cp-legacy nur Roll-Back)

Der Service hält **genau eine** IBKR-Trading-Session offen. Seit v2.0.0
(Karte 5) ist `BG_BACKEND=tws` der Default; der cp-Pfad bleibt unter
Compose-Profile `cp-legacy` für Notfall-Roll-Back verfügbar:

| `BG_BACKEND` | Lifecycle | Status-Werte |
|---|---|---|
| `tws` (Default) | `TWSLifecycle` mit Heartbeat über `ib.isConnected()` + `ib.client.isReady()`; IB Gateway + IBC machen Daily-Restart und Sat-Reset | `ok` / `session_lost` / `tws_down` |
| `cp` (Profile cp-legacy) | `AuthLifecycle` mit Tickle-Job alle 60 s; bis zu 3× `reauthenticate` bei Verlust, sonst `auth_lost` | `ok` / `reauth_pending` / `auth_lost` / `cp_down` |

Konsumenten-Code parsen den stabilen `auth_status_consumer`-View
(`ok | down | lost`), Operations sehen den feinen Backend-Status
über das `auth_status`-Feld. Beide Backends liefern dasselbe Schema
in `/v1/internal/health`. Bei `down`/`lost` antworten Business-
Endpunkte mit `503 Service Unavailable` + `Retry-After: 30`.

Im tws-Mode existiert `app.state.cp_client` bewusst nicht (Hard-Guard
seit v2.0.10 / AP `2a203c58` Phase 6). Konsumenten, die den
cpgateway-HTTP-Client erwarten, sehen einen `AttributeError` statt
einen blinden Call gegen einen nicht-funktionalen Container. Der
cp-spezifische Endpoint `POST /v1/internal/seed-cookies` antwortet
im tws-Mode mit HTTP 503 + `not_applicable_in_tws_mode`.

Der aktuelle Zustand ist über den admin-geschützten Endpunkt abrufbar:

```bash
curl -H "Authorization: Bearer $BG_BOOTSTRAP_ADMIN_TOKEN" \
     http://localhost:8000/v1/internal/health
```

Konfigurierbar über ENV:

| Variable | Default | Wirkung |
|---|---|---|
| `BG_BACKEND` | `tws` | Backend-Auswahl. `tws` = TWS-Socket-API über `ib_async` (Default seit v2.0.0). `cp` = cpgateway-HTTP-Proxy (nur Profile cp-legacy für Notfall-Roll-Back). |
| `BG_CP_BASE_URL` | `http://cpgateway:5000/v1/api` | Base-URL des internen CP Gateways inkl. `/v1/api`-Prefix — der Override **muss** den Suffix enthalten, sonst landen alle Calls in der CP-Gateway-Default-Proxy-Route nach `https://api.ibkr.com` (HTTP 302). |
| `BG_CP_TICKLE_INTERVAL_S` | `60` | Tickle-Intervall in Sekunden (CP-Backend). |
| `BG_TWS_HEARTBEAT_SEC` | `60` | Heartbeat-Intervall in Sekunden (TWS-Backend). |

Definierte Scopes (Single Source of Truth: `src/broker_gateway/auth/models.py`):

| Scope | Berechtigt zu |
|---|---|
| `instruments:read` | Symbol-/conid-Lookup |
| `quotes:read` | Snapshots + Streams |
| `portfolio:read` | Portfolio + Positions + Ledger + Trades |
| `orders:write` | Orders platzieren / canceln + Order-Status |
| `events:read` | Events-Stream |
| `admin:*` | Token-Verwaltung; passt automatisch alle Scope-Checks |

## Observability

Service emittiert pro HTTP-Request ein **JSON-Log-Event** (structlog) mit Metadaten (`request_id`, `method`, `path`, `status`, `latency_ms`, `caller_id`, `scopes`, `idempotency_key`) plus — sofern `BG_LOG_INBOUND_BODIES=on` (Default) — Request-/Response-Headern (gefiltert via `cp/redaction.py`) und Bodies. **Token-Werte werden niemals geloggt** — nur die `caller_id` und die `scopes` aus dem aufgelösten Token; `Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, `X-Auth-Token`, `Proxy-Authorization` werden in jedem Strang gefiltert.

`request_id` wird per `structlog.contextvars.bind_contextvars` gesetzt — damit erscheint sie automatisch in jedem nachgelagerten Event derselben Verarbeitung (z.B. `cp_wire`-Events des CP-Wire-Hooks, siehe unten), und Inbound- und CP-Roundtrips lassen sich darüber korrelieren.

| Feld | Inhalt |
|---|---|
| `request_body` / `response_body` | Geparste JSON-Struktur (bei `application/json`), sonst UTF-8-String, sonst `null` mit `request_body_b64` / `response_body_b64` als Fallback. Bodies werden 1:1 geschrieben — keine Redaction, keine Truncation. |
| `request_headers` / `response_headers` | Gefilterte Header-Map. |
| `response_streaming` | `true` für SSE-Antworten (`text/event-stream`) — der Stream wird nicht materialisiert, `response_body` bleibt `null`. |

### Log-Stränge (forensische Nachvollziehbarkeit)

Sowohl `structlog`-Bound-Logger als auch stdlib-`logging.getLogger()` laufen durch denselben JSONRenderer — jede Log-Zeile ist garantiert ein JSON-Dict, auch aus Modulen wie `throttle`, `streams` oder `cp.lifecycle`.

Routing per Logger-Name auf drei separate Sinks (wenn `BG_LOG_DIR` gesetzt; ohne `BG_LOG_DIR` schreiben alle drei weiter auf stdout — Backwards-Kompatibilität):

| Logger-Name | Datei | Inhalt |
|---|---|---|
| `broker_gateway.http` | `inbound.log` | Consumer → broker-gateway HTTP-Verkehr (heute Metadaten; Bodies kommen mit Folge-Karte AP-05 #2) |
| `broker_gateway.cp.wire` | `cp_wire.log` | broker-gateway → IBKR CP-Gateway HTTP-Roundtrip — 1:1, **ohne** Normalisierung; Default an, abschaltbar via `BG_CP_WIRE_LOG=off` |
| `broker_gateway` | `app.log` | Lifecycle, Throttle, Subscriptions, Streams, Recorder, alles übrige |

`propagate=False` an den drei Strang-Loggern verhindert Cross-Talk. Header-Redaktion (Authorization, Cookie, Set-Cookie, X-API-Key, X-Auth-Token, Proxy-Authorization) lebt zentral in `broker_gateway.cp.redaction` und wird vom CP-Recorder, vom CP-Wire-Logger (`broker_gateway.cp.wire_log.CPWireLogger`) und von der Inbound-Body-Middleware gemeinsam genutzt — Single Source of Truth.

#### CP-Wire-Log

Der `CPWireLogger` hängt sich als httpx-Hook an den CP-Gateway-Client und schreibt pro Roundtrip genau ein `cp_wire`-Event mit `method`, `path`, `query`, `request_headers`, `request_body`, `status`, `response_headers`, `response_body`, `latency_ms`. Bodies werden **nicht** durch `cp.normalize.normalize_response` geschickt — Order-IDs, Timestamps und Session-IDs erscheinen also wie tatsächlich gesendet/empfangen (forensische Treue). Der parallel laufende `CPRecorder` bleibt der einzige Pfad für deterministische Test-Fixtures.

| ENV | Default | Wirkung |
|---|---|---|
| `BG_LOG_DIR` | _leer_ | Leer = stdout (Default). Gesetzt = drei Datei-Sinks unter dem Pfad. |
| `BG_LOG_LEVEL` | `INFO` | Schwellwert für alle drei Stränge. |
| `BG_LOG_ROTATE_MAX_BYTES` | `10485760` (10 MiB) | Größe pro Datei vor Rotation. |
| `BG_LOG_ROTATE_BACKUP_COUNT` | `20` | Anzahl Backup-Dateien pro Strang. |
| `BG_LOG_INBOUND_MAX_BYTES`, `BG_LOG_INBOUND_BACKUP_COUNT` | _Global-Wert_ | Pro-Strang-Override für `inbound.log`. |
| `BG_LOG_CP_WIRE_MAX_BYTES`, `BG_LOG_CP_WIRE_BACKUP_COUNT` | _Global-Wert_ | Pro-Strang-Override für `cp_wire.log`. |
| `BG_LOG_APP_MAX_BYTES`, `BG_LOG_APP_BACKUP_COUNT` | _Global-Wert_ | Pro-Strang-Override für `app.log`. |
| `BG_LOG_INBOUND_BODIES` | `on` | `off` (oder `0`/`false`/`no`) deaktiviert Body- und Header-Felder im `http_request`-Event; Metadaten bleiben unverändert. Notfall-Schalter, falls Bodies zu groß werden. |
| `BG_CP_WIRE_LOG` | `on` | `off` (oder `0`/`false`/`no`) deaktiviert den `CPWireLogger`-Hook am CP-Gateway-Client. Der `CPRecorder` (siehe `BG_CP_RECORD_DIR`) bleibt davon unberührt. |

### Prometheus-Metrics

Prometheus-Scrape-Endpoint unter `GET /metrics` (kein `/v1`-Prefix, im Compose-Setup nur intern publiziert):

| Metrik | Typ | Labels |
|---|---|---|
| `broker_gateway_requests_total` | Counter | `path`, `status`, `scope` |
| `broker_gateway_request_latency_seconds` | Histogram | `path` |
| `broker_gateway_pacing_violations_total` | Counter | `class` |
| `broker_gateway_session_age_seconds` | Gauge | — |
| `broker_gateway_subscription_count` | Gauge | — |
| `broker_gateway_throttle_extra_wait_seconds` | Gauge | `class` |

Die Gauges werden per Custom-Collector beim Scrape live aus den Singletons (AuthLifecycle, SubscriptionManager, ThrottleManager) gelesen — keine Stale-State-Probleme.

```bash
curl http://localhost:4000/metrics
```

## Tests

Tests laufen strikt in-process gegen einen Mock des internen IBKR Client
Portal Gateways. Es werden niemals echte HTTP-Calls nach außen gemacht und
es ist kein laufendes CP-Gateway erforderlich.

```bash
pytest
```

Die Fixture `cp_gateway_mock` (definiert in `tests/conftest.py`) ist die
**Single Source of Truth** für Mock-Antworten. Statische Bodies werden ab
v1.2.0 aus `tests/fixtures/recorded/` geladen (`live/` hat Vorrang vor
`seed/`); stateful Endpunkte (snapshot, orders-Lifecycle, trades-Schleife,
unsubscribe) generieren ihre Antworten weiterhin im Code in
`tests/cp_mock/replay.py`. Folge-Karten dürfen keine eigenen Mocks
definieren, sondern konfigurieren stattdessen Flags am Fixture-Objekt:

```python
def test_quote_snapshot_with_auth_loss(cp_gateway_mock):
    cp_gateway_mock.auth_lost = True
    # eigentlicher Test gegen broker_gateway-Code, der unter der Haube
    # einen httpx-Client gegen cp_gateway_mock.base_url instantiiert
```

Konfigurierbare Flags:

| Flag | Wirkung |
|------|---------|
| `auth_lost` | `/iserver/auth/status` liefert `authenticated=false` |
| `slow_response_ms` | künstliche Latenz pro Request in Millisekunden |
| `pacing_violation_after_n` | nach N Requests HTTP 429 für jeden weiteren |

Live-Tests gegen ein echtes CP-Gateway (z.B. lokal über Docker Desktop)
laufen ausschließlich außerhalb der pytest-Suite und sind nicht Teil
des Default-Workflows.

## Recordings

Ab v1.1.0 kann der `CPGatewayClient` Live-HTTP-Verkehr als deterministische
JSON-Fixtures unter `tests/fixtures/recorded/` ablegen. Aktivierung
ausschließlich über die Environment-Variable `BG_CP_RECORD_DIR` — im
Produktiv-Default ist der Recorder nicht aktiv und verursacht keine
Disk-IO. Authorization-, Cookie- und API-Key-Header werden vor dem
Schreiben gefiltert; Timestamps und Order-/Execution-/Session-IDs in
Bodies werden durch Platzhalter ersetzt. Konzept, Naming, Diff-Bewertung:
[`docs/cp-recordings.md`](docs/cp-recordings.md). Die Fixtures dienen ab
AP-02 #03 als Single Source of Truth des Mock-Replays — handgeschriebene
Mock-Antworten werden schrittweise abgelöst.

## Container-Stack

```bash
docker compose up -d
curl http://localhost:4000/v1/health
```

Stack besteht aus `gateway` (FastAPI, intern 8000, extern 4000) und
`tws` (`gnzsnz/ib-gateway:stable` mit IBC + Xvfb für IB Gateway 10.46,
Java-API auf Port 4002 intern). Beim Lifespan-Start wählt der Service
den Backend-Pfad über `BG_BACKEND` — Default `tws` seit v2.0.0. Der
`cpgateway`-Service ist unter Compose-Profile `cp-legacy` definiert
und wird nur bei `--profile cp-legacy` mitgestartet (Roll-Back für
den Notfall). Vollständige Deploy-Anleitung — Pfad-Konventionen,
Workflow, Restart-Disziplin, Recovery nach Saturday-Reset, Rollback,
ENV-Variablen Live vs Paper — in
[`docs/03-deployment.md`](docs/03-deployment.md). Login mit
Browser-2FA für den cp-Pfad (Notfall):
[`docs/runbooks/cpgateway-login.md`](docs/runbooks/cpgateway-login.md).
Troubleshooting: [`docs/runbooks/cpgateway-troubleshooting.md`](docs/runbooks/cpgateway-troubleshooting.md).

## Warum dieser Service existiert, Boundary, Stack

IBKR erlaubt nur **eine Trading-Session pro Konto**. `broker-gateway` ist
der einzige Halter dieser Session und exponiert sie gemultiplext über
`/v1`. Vollständige Begründung, Architektur-Prinzipien (Singular-Halter,
Stateless-Außen / Stateful-Innen, Idempotency, Transient,
Single-Source-of-Truth), Komponenten-Übersicht und Was-NICHT-in-v1 in
[`docs/02-architecture.md`](docs/02-architecture.md). Konsumenten-Mapping
siehe Sektion 6 dort und [Authentifizierung](#authentifizierung) oben.

## Verwandte Projekte

- [personal_stock_manager](https://github.com/machdat/personal_stock_manager) — Portfolio-Kurator, Consumer dieser API.
- trading-robot — autonomer Trader, Consumer dieser API (in Entwicklung, eigenes Repo).
- IBKR Client Portal Gateway — als interner Sub-Container, kein direkter Consumer-Kontakt.

## Lizenz

Noch nicht festgelegt.

---

*Version 2.2.0*
