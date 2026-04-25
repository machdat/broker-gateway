# broker-gateway

Versionierte HTTP-API zwischen Consumern (PSM, trading-robot, ad-hoc CLI/Notebooks) und broker-vermittelten Diensten — Aktienhandel und Marktdaten-Streaming. Aktuell adaptiert ausschließlich **Interactive Brokers** über das Client Portal Gateway als interne Sub-Komponente. Das ist Absicht und kein Marketing-Versprechen für später: der Service entkoppelt Consumer von IBKR-Spezifika, damit das Adapter-Backend austauschbar bleibt, ohne dass `/v1` brechen muss.

**Status:** Deployed mit `/v1/health` (v0.1.0), pytest-Mock-Fixture für das interne CP-Gateway (v0.2.0), Auth-Modell mit Token-Management (v0.3.0), CP-Gateway-Auth-Lifecycle inkl. `/v1/internal/health` (v0.4.0), Instruments-Lookup mit Symbol-Cache (v0.5.0), Quotes-Snapshot mit First-Call-Prime + Availability-Normalisierung (v0.6.0), SSE-Quotes-Stream mit Refcount + Fan-Out (v0.7.0), Portfolio-Endpunkten (Summary/Positions/Ledger) mit Money-Normalisierung (v0.8.0), Order-Lifecycle mit Idempotency-Key + Reply-Confirmation-Loop (v0.9.0), Trades-History inkl. MTD-Commission-Aggregat (v0.10.0), Events-Stream (SSE) für Execution/Position/Status mit EventBus + Last-Event-ID-Reconnect (v0.11.0) und Rate-Limit-Throttle mit Token-Bucket pro Endpoint-Klasse + Pacing-Violation-Backoff (v0.12.0). Weitere Endpoints folgen über das KanProject `broker-gateway`.

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
| `BG_CP_BASE_URL` | `http://cpgateway:5000` | Base-URL des internen CP Gateways |
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

## Tests

Tests laufen strikt in-process gegen einen Mock des internen IBKR Client
Portal Gateways. Es werden niemals echte HTTP-Calls nach außen gemacht und
es ist kein laufendes CP-Gateway erforderlich.

```bash
pytest
```

Die Fixture `cp_gateway_mock` (definiert in `tests/conftest.py`) ist die
**Single Source of Truth** für Mock-Antworten — Folge-Karten dürfen keine
eigenen Mocks definieren, sondern konfigurieren stattdessen Flags am
Fixture-Objekt:

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

## Container-Stack

```bash
docker compose up -d
curl http://localhost:4000/v1/health
```

Der Stack besteht aus zwei Services: `gateway` (FastAPI-App) und `cpgateway`
(IBKR Client Portal Gateway, in v0.1.0 noch Platzhalter — die echte
Integration folgt in einer spaeteren Karte). Extern publiziert auf Port 4000
(intern 8000), passend zum geplanten Default `broker-gateway:4000` aus
`docs/api/v1-draft.md` Section 1.1.

## Warum dieser Service existiert

IBKR erlaubt nur **eine Trading-Session pro Konto**. Sobald PSM und trading-robot beide direkt mit dem CP Gateway sprechen würden, würden sie sich gegenseitig die Session abschießen. Außerdem hat IBKR clientseitige Rate-Limits (~50 Nachrichten/s pro Konto), Subscription-State ist global pro Session, und der Auth-/Tickle-Lifecycle erfordert einen einzigen langlaufenden Halter.

`broker-gateway` ist dieser eine Halter. Consumer reden gegen eine HTTP-API mit Authorization-Token und Scope-Claims, der Service queueuet, throttelt, refcountet Subscriptions und fan-outed Streams.

## Boundary

**In Scope**
- Single-Owner der IBKR-Trading-Session.
- Authorization via API-Token mit Scope-Claims (`quotes:read`, `portfolio:read`, `orders:write`, `events:read`).
- Subscription-Refcount + Fan-Out für Marktdaten-Streams (SSE/WebSocket).
- Idempotency-Keys für Orders.
- Rate-Limit-Throttle, Reconnect, Reauthenticate.
- Versionierte API: `/v1` heute, `/v2` erst bei echter Breaking-Change.

**Out of Scope**
- Portfolio-Logik, Scoring, Trading-Strategie — gehört zu Consumern.
- Persistente Geschäftsdaten — Service ist transient (In-Memory-Caches, optional Redis für Restart-Persistenz).
- Frontend/UI — nur API.
- Multi-Broker-Adapter zunächst nicht.

## Konsumenten

| Consumer | Erwartete Scopes |
|---|---|
| **personal_stock_manager** (PSM) | `quotes:read`, `portfolio:read`, `instruments:read` (kein `orders:write`) |
| **trading-robot** | `quotes:read`, `portfolio:read`, `instruments:read`, `orders:write`, `events:read` |
| Admin-CLI / Ad-hoc-Tools | konfigurierbar, mit Rotation |

## Stack (Plan, nicht final)

- Python 3.12 + FastAPI (analog PSM).
- httpx für interne CP-Gateway-Calls.
- SSE oder WebSocket für Stream-Endpoints (Entscheidung in erster Karte).
- Docker Compose mit zwei Services: `gateway` (eigener Code) und `cpgateway` (IBKR CP Gateway, eclipse-temurin:21-jre-noble).
- pytest, In-Memory Mock-CP-Gateway für Tests.

## Verwandte Projekte

- [personal_stock_manager](https://github.com/machdat/personal_stock_manager) — Portfolio-Kurator, Consumer dieser API.
- trading-robot — autonomer Trader, Consumer dieser API (in Entwicklung, eigenes Repo).
- IBKR Client Portal Gateway — als interner Sub-Container, kein direkter Consumer-Kontakt.

## Lizenz

Noch nicht festgelegt.

---

*Version 0.12.0*
