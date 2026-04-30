# broker-gateway

Versionierte HTTP-API zwischen Consumern (PSM, trading-robot, ad-hoc CLI/Notebooks) und broker-vermittelten Diensten — Aktienhandel und Marktdaten-Streaming. Aktuell adaptiert ausschließlich **Interactive Brokers** über das Client Portal Gateway als interne Sub-Komponente. Das ist Absicht und kein Marketing-Versprechen für später: der Service entkoppelt Consumer von IBKR-Spezifika, damit das Adapter-Backend austauschbar bleibt, ohne dass `/v1` brechen muss.

**Status:** v1.11.0 — Service deployed auf cma-pi-1 (Port 4000), AP-01..AP-03 abgeschlossen, AP-04 (WS-Discovery) Phase 1 durch K1..K4 fertig, AP-05 (Logging) K1+K2 live. Aktueller Architektur-Stand und Komponenten-Übersicht in [`docs/02-architecture.md`](docs/02-architecture.md). Vollständige Versionshistorie in [`CHANGELOG.md`](CHANGELOG.md).

## Architektur und Doku

| Frage | Lebt in |
|---|---|
| Wie ist der Service intern gebaut? | [`docs/02-architecture.md`](docs/02-architecture.md) |
| Welche Endpunkte gibt es, mit welchen Bodies/Headern? | [`docs/api/v1-draft.md`](docs/api/v1-draft.md) |
| Wie deploye / login ich CP-Gateway? | [`docs/runbooks/cpgateway-login.md`](docs/runbooks/cpgateway-login.md) |
| Welche IBKR-CP-API-Details liegen hinter Feld X? | [`docs/research/`](docs/research/) |
| Was war beim Bootstrap entschieden? | [`docs/01-context-from-bootstrap-session.md`](docs/01-context-from-bootstrap-session.md) |

Erste Anlaufstelle für neue Sessions ist `docs/02-architecture.md`. Alles weitere unten ist Quickstart und operationelle Referenz.

## Lokal starten

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows / Git-Bash: source .venv/Scripts/activate
pip install -e .[dev]
pytest
uvicorn broker_gateway.main:app --reload
curl http://localhost:8000/v1/health
```

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

## CP-Gateway-Auth-Lifecycle

Der Service hält **genau eine** IBKR-Trading-Session offen und betreibt
im Hintergrund einen Tickle-Job (`asyncio`). Bei verlorener Session wird
bis zu dreimal `reauthenticate` versucht, sonst kippt der Status auf
`auth_lost` und alle Business-Endpunkte (Quotes/Orders/...) liefern ab
Karte 06 `503 Service Unavailable` + `Retry-After: 30`.

Der aktuelle Zustand ist über den admin-geschützten Endpunkt abrufbar:

```bash
curl -H "Authorization: Bearer $BG_BOOTSTRAP_ADMIN_TOKEN" \
     http://localhost:8000/v1/internal/health
```

Konfigurierbar über ENV:

| Variable | Default | Wirkung |
|---|---|---|
| `BG_CP_BASE_URL` | `http://cpgateway:5000/v1/api` | Base-URL des internen CP Gateways inkl. `/v1/api`-Prefix — der Override **muss** den Suffix enthalten, sonst landen alle Calls in der CP-Gateway-Default-Proxy-Route nach `https://api.ibkr.com` (HTTP 302). |
| `BG_CP_TICKLE_INTERVAL_S` | `60` | Tickle-Intervall in Sekunden |

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

`request_id` wird per `structlog.contextvars.bind_contextvars` gesetzt — damit erscheint sie automatisch in jedem nachgelagerten Event derselben Verarbeitung (z.B. `cp_wire`-Events des kommenden CP-Wire-Hooks), und Inbound- und CP-Roundtrips lassen sich darüber korrelieren.

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
| `broker_gateway.cp.wire` | `cp_wire.log` | broker-gateway → IBKR CP-Gateway HTTP-Roundtrip (Hook kommt mit AP-05 #3) |
| `broker_gateway` | `app.log` | Lifecycle, Throttle, Subscriptions, Streams, Recorder, alles übrige |

`propagate=False` an den drei Strang-Loggern verhindert Cross-Talk. Header-Redaktion (Authorization, Cookie, Set-Cookie, X-API-Key, X-Auth-Token, Proxy-Authorization) lebt zentral in `broker_gateway.cp.redaction` und wird vom CP-Recorder (und künftig CP-Wire-Logger sowie Inbound-Body-Middleware) gemeinsam genutzt — Single Source of Truth.

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

Der Stack besteht aus zwei Services: `gateway` (FastAPI-App) und `cpgateway`
(IBKR Client Portal Gateway, ab v1.0.1 scharfgeschaltet — Build aus
`Dockerfile.cpgateway` mit Tarball aus `ops/cpgateway/clientportal.gw.tar.gz`,
siehe Abschnitt **CP-Gateway live betreiben**). Extern publiziert auf Port 4000
(intern 8000), passend zum geplanten Default `broker-gateway:4000` aus
`docs/api/v1-draft.md` Section 1.1. Der `cpgateway`-Service ist nicht extern
publiziert; `gateway` wartet via `depends_on: condition: service_healthy` auf
einen healthy CP-Gateway-Container.

## CP-Gateway live betreiben

Der IBKR Client Portal Gateway läuft als interner Container im Compose-Stack.
Beim ersten Start ist eine manuelle Browser-Anmeldung mit 2FA (Konto
**U25235077**) erforderlich; danach hält der `gateway`-Service die Session
über den Tickle-Lifecycle warm.

- **Setup-Anleitung:** `ops/cpgateway/README.md` (Tarball-Download, SHA256, Layout).
- **Login-Runbook:** `docs/runbooks/cpgateway-login.md` (SSH-Tunnel, Browser-Login, Validierung).
- **Troubleshooting:** `docs/runbooks/cpgateway-troubleshooting.md` (sechs typische Fehlerbilder mit Fix).

Der `cpgateway`-Container läuft ab v1.0.3 als non-root-User `cpgw`. UID/GID
werden über die Build-Args `CPGW_UID`/`CPGW_GID` (Default 1000) gesetzt und
in `compose.yaml` aus den gleichnamigen Environment-Variablen / `.env`-Werten
gelesen. Auf einem Host, dessen Betriebs-User eine andere UID hat als 1000,
müssen die Werte in `.env` gepflegt werden — sonst gehören die Log-Dateien
unter `var/cpgateway/logs/` einem im Host nicht existierenden User. Prüfen mit
`id cma`.

Der Tarball `clientportal.gw.tar.gz` wird **nicht** versioniert. Eingecheckt
wird ausschließlich die SHA256-Prüfsumme (`ops/cpgateway/clientportal.gw.tar.gz.sha256`),
die im Image-Build strikt verifiziert wird.

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

*Version 1.11.0*
