# 01 — Bootstrap-Kontext (historisches Session-Protokoll)

> **Hinweis:** Architektur-Inhalte (Singular-Halter, Stateless-Außen,
> IBKR-Adaptions-Schicht, API-Universum, Was bewusst NICHT in v1) sind
> nach [`docs/02-architecture.md`](02-architecture.md) verschoben und
> werden dort gepflegt. Dieses Dokument bleibt als Session-Protokoll
> 2026-04-23/24 erhalten — wer den Architektur-Stand sucht, lese
> stattdessen `02-architecture.md`. Sektionen 1, 3, 4, 5, 6 wurden
> entfernt (siehe AP-09 K1, 2026-04-30). Was hier bleibt: Konsumenten-
> Bedürfnisse, Karten-Schnitt-Vorschläge aus der Session, offene
> Tech-Entscheidungen zum Zeitpunkt des Bootstraps, PSM-Memory-Bezug
> und der Bootstrap-Snapshot-Zustand.

**Datum:** 2026-04-23 / 2026-04-24
**Quelle:** Bootstrap-Session zwischen User und Claude Code im Rahmen der PSM-Karte `136b7881` (IBKR-Marktdaten-Verifikation) und Folge-Diskussion zur Architektur-Ausgliederung.
**Zweck (damals):** Kontext-Grundlage für die Erstellung der ersten Implementierungs-Karten in AP-01 Foundation.

---

## 2. Zielkonsumenten und ihre Bedürfnisse

### 2.1 PSM (personal_stock_manager)

- Liest Portfolio (Holdings, Marktwerte).
- Liest aktuelle Marktdaten (Snapshot oder Stream je nach Use-Case).
- Liest historische Trades (für KESt-Berechnung, FIFO).
- **Schreibt keine Orders.** Sollte Authorization-Token mit Scopes `quotes:read`, `portfolio:read`, `instruments:read`, `events:read` haben, **kein** `orders:write`.

### 2.2 trading-robot

- Reagiert auf Marktdaten-Streams (Stream-Subscription).
- Platziert Orders nach eigenen Strategien.
- Liest Portfolio für Position-Sizing.
- Braucht alle Scopes inkl. `orders:write`.

### 2.3 Admin / CLI / Notebooks

- Ad-hoc-Diagnose, Datenextraktion.
- Token-Rotation, Service-Health-Checks.
- Optional: `admin:*` Scope, kurzlebige Tokens.

---

## 7. Empfohlene Karten-Schnitte für AP-01 (Stand Bootstrap)

Vorschlag aus der Bootstrap-Session, wie Cowork aus diesem Kontext die ersten Karten generieren könnte. Inzwischen ist AP-01 abgeschlossen (alle Karten 1-12 done) — diese Liste wird nur noch als Historie geführt:

1. **Container-Skelett + Repo-Layout** — pyproject, src/-Tree, Dockerfile, docker-compose mit gateway+cpgateway, basic FastAPI-App mit `/v1/health`. Erster Deploy auf cma-pi-1.
2. **Auth-Modell + Token-Management** — Token-Generierung, Scope-Claims, Middleware, `/v1/auth/token`-Endpoint, Revoke. Tests.
3. **Instruments-Lookup** — Symbol→conid-Cache, `/v1/instruments/search`, `/v1/instruments/{conid}`.
4. **Quotes-Snapshot mit First-Call-Prime** — `/v1/quotes/snapshot`, intern doppelter Call, Availability-Normalisierung.
5. **Quotes-Stream (SSE) + Subscription-Refcount** — `/v1/quotes/stream`, Fan-Out aus einer einzigen IBKR-Subscription pro conid.
6. **Portfolio-Endpoints** — `/v1/portfolio/{accountId}`, Positions, Ledger. Cached mit Max-Age.
7. **Order-Lifecycle mit Idempotency** — `POST /v1/orders`, `GET /v1/orders/{id}`, `DELETE /v1/orders/{id}`. Idempotency-Key-Mapping.
8. **Trades-History** — `/v1/trades`, Aggregations.
9. **Events-Stream (SSE)** — `/v1/events/stream`, Execution-Reports.
10. **Auth-Lifecycle-Recovery** — Detect/Recover bei Session-Kick, `Retry-After` bei Re-Login-Bedarf.
11. **Rate-Limit-Throttle** — Token-Bucket pro Endpoint-Klasse, Pacing-Violation-Backoff.
12. **Observability** — Structured Logs, Prometheus-Metrics, Tracing.
13. **Mock-CP-Gateway-Fixture für Tests** — pytest-Plugin, deterministische Responses, Subscription-Simulation.
14. **PSM-Migration auf v1-API** — PSM-seitig die direkten CP-Gateway-Calls auf broker-gateway-Calls umstellen. (Liegt in PSM-Backlog, nicht hier — aber Coordination-Karte.)

Diese Reihenfolge respektierte Abhängigkeiten und lieferte nach Karte 1 schon ein deploybares Skelett. Karten 2-9 wurden weitgehend parallelisiert.

---

## 8. Technologie-Entscheidungen, die zum Zeitpunkt des Bootstraps offen waren

Die Antworten dazu sind inzwischen umgesetzt — Stand wird hier als Historie geführt; aktuelle Architektur-Entscheidungen siehe `docs/02-architecture.md`:

- **Stream-Transport: SSE oder WebSocket?** Bootstrap-Empfehlung: SSE für `/v1/quotes/stream` und `/v1/events/stream`. Reconnect mit `Last-Event-ID`-Header. **Umgesetzt:** SSE in v1.7.0 / v1.11.0; WS-Adapter ist seit AP-04 in Discovery (siehe Architektur-Doku Sektion 7).
- **Token-Format: JWT oder Opaque?** Bootstrap-Empfehlung: Opaque mit Redis-Lookup oder einfacher JSON-Datei. **Umgesetzt:** Opaque-Tokens in Memory- oder File-Store (`BG_TOKEN_FILE`).
- **Idempotency-Storage: Memory, Redis, SQLite?** Bootstrap-Empfehlung: Memory mit konfigurierbarer TTL. **Umgesetzt:** In-Memory-Map in `idempotency.py`.
- **Reverse-Proxy / TLS?** Bootstrap-Stand: lokal HTTP, später extern Caddy/nginx mit Let's Encrypt. **Status:** intern HTTP über Tailscale, externer TLS-Endpunkt steht weiter zur Diskussion (siehe Architektur-Doku Sektion 11.2 offene Fragen).
- **Compose vs. Standalone-Container?** Bootstrap-Empfehlung: Compose. **Umgesetzt:** zwei Services `gateway` + `cpgateway` in einem Compose-Stack.

---

## 9. Bezug zu PSM-Memory (Übertragungs-Kandidaten)

Diese PSM-Memories sind sinngemäß für broker-gateway relevant, sollten in dessen eigene Auto-Memory übertragen werden, sobald sie einmal session-übergreifend gebraucht werden:

- `project_pi_deployment` — Deploy-Workflow auf cma-pi-1, Pfade.
- `feedback_deploy_workflow` — "deployen" = Branch → Commit → PR → Merge → SSH-Deploy.
- `feedback_bash_hook_cma_pi_1` — Bash-Hook blockiert `cma-pi-1` direkt, PowerShell als Workaround.
- `feedback_version_bump` — Jede Änderung braucht Version-Bump.
- `feedback_independent_card_deploys` — Karten self-contained deploybar.
- `feedback_autonomous_work` — Karten ohne Rückfragen abarbeiten, nur bei Entscheidungen unterbrechen.
- `feedback_mock_tests_miss_schema` — vor jedem Mock prüfen, ob das gemockte Verhalten real existiert.

---

## 10. Aktueller Zustand zum Zeitpunkt des Bootstraps (Snapshot 2026-04-24)

- Repo: angelegt, public, leer bis auf README + CLAUDE.md + .gitignore.
- KanPrompt-Projekt: angelegt, ID `a6a45428-ac37-48f5-b295-d3ff26f31711`, Farbe green.
- AP-01: dieses Arbeitspaket, ID `411c3b19-7737-4a50-a9ff-5c521a302372`.
- Karten: noch keine. Werden auf Basis dieses Dokuments durch Cowork erstellt.
- Implementierung: noch keine.
- Deploy-Target: cma-pi-1, Pfad noch zu wählen (Vorschlag `/mnt/ssd/broker-gateway`).

> Inzwischen: AP-01 abgeschlossen (v1.0.0), Service deployed auf cma-pi-1
> unter `/mnt/ssd/broker-gateway`, Port 4000. AP-02 (Record-and-Replay)
> abgeschlossen, AP-04 (WS-Discovery) Phase 1 durch K1..K4 abgeschlossen.
> AP-05 K1+K2 (Logging-Backbone, Inbound-Body-Logging) deployed.
> Aktueller Stand siehe README + `docs/02-architecture.md`.

---

*Version 1.0 — Bootstrap-Kontext, 2026-04-24.
Architektur-Inhalte am 2026-04-30 nach `docs/02-architecture.md` ausgelagert (AP-09 K1).*
