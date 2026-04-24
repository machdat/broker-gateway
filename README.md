# broker-gateway

Versionierte HTTP-API zwischen Consumern (PSM, trading-robot, ad-hoc CLI/Notebooks) und broker-vermittelten Diensten — Aktienhandel und Marktdaten-Streaming. Aktuell adaptiert ausschließlich **Interactive Brokers** über das Client Portal Gateway als interne Sub-Komponente. Das ist Absicht und kein Marketing-Versprechen für später: der Service entkoppelt Consumer von IBKR-Spezifika, damit das Adapter-Backend austauschbar bleibt, ohne dass `/v1` brechen muss.

**Status:** Erste deploybare Iteration (`/v1/health`). Weitere Endpoints folgen ueber das KanProject `broker-gateway`.

## Lokal starten

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows / Git-Bash: source .venv/Scripts/activate
pip install -e .[dev]
pytest
uvicorn broker_gateway.main:app --reload
curl http://localhost:8000/v1/health
```

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

*Version 0.1.0*
