# 01 — Bootstrap-Kontext

**Datum:** 2026-04-23 / 2026-04-24
**Quelle:** Bootstrap-Session zwischen User und Claude Code im Rahmen der PSM-Karte `136b7881` (IBKR-Marktdaten-Verifikation) und Folge-Diskussion zur Architektur-Ausgliederung.
**Zweck:** Kontext-Grundlage für die Erstellung der ersten Implementierungs-Karten in AP-01 Foundation. Cowork (oder ein anderer Karten-Autor) sollte diese Punkte in die jeweiligen Karten als Hintergrund-Referenz einarbeiten.

---

## 1. Warum dieses Projekt überhaupt existiert

### 1.1 IBKR Single-Session-Constraint

IBKR erlaubt **eine einzige Trading-Session pro Konto**, nicht pro Benutzer. Sobald zwei Komponenten (z.B. PSM-App und trading-robot) parallel mit demselben Konto am IBKR-CP-Gateway hängen würden, kickt sich das eine das andere weg. In unserer Bootstrap-Session ist das real passiert: Browser-Login ins IBKR-Kundenportal hat die laufende CP-Gateway-Session invalidiert (HTTP 401 nach Re-Login).

**Konsequenz:** Es muss eine einzelne Komponente geben, die exklusiv die IBKR-Session hält. Alle anderen Komponenten müssen über diese Komponente sprechen.

### 1.2 Rate-Limit-Realität

IBKR throttelt pro Konto auf ungefähr **50 Nachrichten/Sekunde** über alle gleichzeitigen Verbindungen. Bei der Web-API ist das pro Endpoint nochmal differenziert (Snapshot anders als Orders anders als Subscriptions). Mehrere unkoordinierte Caller produzieren `Pacing violation` und Verbindungsabbrüche.

**Konsequenz:** Eine zentrale Queue serialisiert Requests. Nur diese Queue weiß, wie viele Requests aktuell offen sind.

### 1.3 Subscription-State ist global pro Session

Marktdaten-Subscriptions im CP Gateway sind **session-global**, nicht caller-spezifisch. Wenn Caller A `AAPL` abonniert und Caller B den unsubscribt, verliert A unbemerkt seinen Stream. Es braucht **Refcounting**: ein Symbol bleibt subscribed solange mindestens ein Consumer es will.

### 1.4 Auth-/Tickle-Lifecycle

CP Gateway braucht alle ~60 Sekunden einen `POST /tickle`, sonst läuft die Session aus. Browser-Login muss vor Ablauf erneuert oder per `reauthenticate` aufgefrischt werden. Das ist nicht-trivialer Zustand, den nur eine Stelle halten darf.

### 1.5 Empirisch belegte Beobachtungen

In der Session 2026-04-23 mehrfach reproduziert:
- **First-Call-Primes-Subscription:** der erste Snapshot-Call liefert leere Payload `[{conidEx, conid}]`, der zweite Call (nach ~3 s) liefert echte Werte.
- **6509-Availability-Code:** drei Zeichen (z.B. `DPB` = Delayed/Paid/Book, `RPB` = Realtime/Paid/Book). Entscheidet ob Marktdaten realtime oder delayed sind, **unabhängig** vom Portal-"aktiv"-Listing.
- **Konto-Reifung:** Non-Pro-US-Realtime-Streams sind im Portal als "aktiv" gelistet, werden aber erst nach 30 Tagen Kontoalter oder nach Erreichen der USD-30-MTD-Commission-Waiver tatsächlich freigeschaltet. Bis dahin liefert der Endpoint Delayed-Daten (Flag `D*`).
- **whatif-Order-Risk-Subsystem** prüft eine andere Marktdaten-Flag als der Snapshot-Endpoint. Solange Realtime nicht freigeschaltet ist, liefert whatif Warnings 4 + 21 ("Percentage price check cannot be performed", "blind trading"), auch wenn der Snapshot brauchbare Werte liefert.
- **Account-Endpoint-Permissions:** `allowedAssetTypes: STK,OPT,...` listet, was der Account-Typ grundsätzlich könnte. Für tatsächliche Trading-Permissions: pro Asset-Klasse separate Anfrage im Portal (User U25235077 aktuell: Aktien + Währungen/Devisen aktiv, alles andere Anfrage-pflichtig).
- **MTD-Commissions** lassen sich aus `/iserver/account/trades?days=30` aggregieren (Summe `commission`-Feld), aber das Feld hat keine Währungsangabe — bei mehrheitlich US-Aktien plausibel USD.

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

## 3. Architektur-Prinzipien (verbindlich)

### 3.1 Singular-Halter

Es gibt **genau eine** Instanz von broker-gateway pro IBKR-Konto. Das ist keine Skalierungs-Entscheidung, sondern eine Hard-Constraint von IBKR. Skalierung passiert auf Consumer-Seite, nicht hier.

### 3.2 Stateless-Außen, Stateful-Innen

Nach außen verhält sich die API wie ein REST-Service: jeder Request ist self-contained (Token im Header, alle nötigen Parameter im Body/Query). Innen hält der Service Auth-Session, Subscription-State, Idempotency-Map, Order-Cache. Diese Trennung erlaubt einfache Consumer-Implementierungen.

### 3.3 Versioniert am Contract, nicht am Code

`/v1` muss rückwärtskompatibel bleiben, solange er angeboten wird. Additive Felder in Responses sind erlaubt. Breaking Changes ausschließlich in `/v2`. `/v1` und `/v2` können parallel laufen, mit `Deprecation`-Header bei Ablauf-Plan.

### 3.4 Idempotency für Schreiboperationen

Jede Schreiboperation (Order, Cancel) erfordert einen `Idempotency-Key` im Header. Der Service speichert das Mapping `key → response` für eine konfigurierbare TTL (Default 24 h). Wiederholungen liefern identische Responses ohne erneuten Broker-Call. **Schutz vor Duplicate-Orders bei Netzwerk-Retries.**

### 3.5 Transient

Kein Business-Persistenz-Layer. Session-State, Cache, Subscription-Map liegen im Memory. Optional Redis als externer State-Backing für Restart-Persistenz, aber kein eigenes Schema, kein Alembic. Datenmodell des Services ist die API selbst.

### 3.6 Observability eingebaut

- Strukturierte Logs (JSON, ein Event pro Request inkl. caller_id, scope, latency).
- Prometheus-Metriken: Request-Count/Latency pro Endpoint, IBKR-Session-Age, Subscription-Count, Queue-Depth, Pacing-Violations.
- Healthcheck mit unterscheidbaren Failure-Modes (auth_lost, ibkr_down, queue_overflow).

---

## 4. IBKR-spezifische Details, die intern gelöst werden müssen

| Problem | Lösung im Gateway |
|---|---|
| First-Call leerer Snapshot | Server primt intern: bei Snapshot-Anfrage zwei Calls hintereinander, gibt nur den zweiten zurück. Consumer sieht immer Daten. |
| Tickle alle 60 s | Hintergrund-Job, läuft solange Auth-Session aktiv. |
| Browser-2FA-Login | Initial einmalig durch User. Service erkennt invalide Session und exposed `/internal/auth/status` mit dem Hinweis, dass Re-Login nötig ist. Consumer-API gibt 503 mit `Retry-After`. |
| Session-Kicked durch Portal-Login | Service detected, versucht reauthenticate, sonst 503. Im Health-Endpoint klar ausweisen. |
| Subscription-Limits (max ~5 conids pro snapshot-Call, max ~250 streams pro session) | Server-seitig mit Queue + Refcount + ggf. Multi-Snapshot-Aggregation. Consumer kümmert sich nicht. |
| Symbol↔conid-Mapping | Cache mit langer TTL (Symbole wechseln ihre conid praktisch nie). Lookup-Endpoint. |
| Currency-Unterscheidung in Order-Responses | Explizit normalisieren: jedes Geldfeld bekommt `value` + `currency`. |
| 6509-Availability-Code | Im Quote-Response in eigenem Feld `availability` mit semantischer Übersetzung (`realtime` / `delayed` / `frozen`). Roher Code zusätzlich für Debug. |

---

## 5. Was bewusst NICHT in v1 gehört

Diese Punkte sind aufgekommen und wurden bewusst aus v1 ausgeklammert:

- **Multi-Account-Support.** v1 spricht mit genau einem Konto. Multi-Account braucht eine zweite Service-Instanz pro Konto.
- **Multi-Broker-Support.** v1 ist hardcoded auf IBKR. Adapter-Pattern für andere Broker ist denkbar in v2/v3, aber jetzt YAGNI.
- **Historische Marktdaten** (Bars, EOD-Series). PSM nutzt yfinance + andere Sources. broker-gateway ist Realtime-fokussiert.
- **Options-Chains, FOPs, Futures** in v1. Erst wenn der User entsprechende Permissions hat. Zunächst Stocks + Cash (FX).
- **Order-Routing-Strategien** (Smart-Routing-Konfigurationen, OCA-Gruppen). v1 nimmt einfache Order-Typen (LMT, MKT, STP, STP-LMT) und routet via IBKR-Default.
- **Komplexe Auth-Pipelines** (OAuth, OIDC). v1 nutzt simple opaque API-Tokens mit Scope-Claims, intern in Datei oder ENV.

---

## 6. Erwartetes API-Universum (high-level)

Detail-Spec siehe `docs/api/v1-draft.md`. Hier nur die Konzept-Liste für Karten-Erstellung:

| Bereich | Endpoints (grob) |
|---|---|
| Auth | `POST /v1/auth/token`, `DELETE /v1/auth/token` (Revoke) |
| Health | `GET /v1/health`, `GET /v1/internal/health` (mit IBKR-Detail) |
| Instruments | `GET /v1/instruments/search`, `GET /v1/instruments/{conid}` |
| Quotes | `GET /v1/quotes/snapshot?conids=...&fields=...`, `GET /v1/quotes/stream` (SSE) |
| Portfolio | `GET /v1/portfolio/{accountId}`, `GET /v1/portfolio/{accountId}/positions`, `GET /v1/portfolio/{accountId}/ledger` |
| Orders | `POST /v1/orders` (Idempotency-Key Pflicht), `GET /v1/orders/{orderId}`, `DELETE /v1/orders/{orderId}` |
| Trades | `GET /v1/trades?from=...&to=...` |
| Events | `GET /v1/events/stream` (SSE: Execution-Reports, Position-Updates, Status-Changes) |

---

## 7. Empfohlene Karten-Schnitte für AP-01

Vorschlag, wie Cowork aus diesem Kontext die ersten Karten generieren könnte. Jede Karte ist self-contained deploybar (analog PSM-Memory `feedback_independent_card_deploys`):

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

Diese Reihenfolge respektiert Abhängigkeiten und liefert nach Karte 1 schon ein deploybares Skelett. Karten 2-9 können größtenteils parallelisiert werden.

---

## 8. Technologie-Entscheidungen, die noch zu treffen sind

Diese Fragen sollten in den ersten Karten als `decision` geloggt werden:

- **Stream-Transport: SSE oder WebSocket?** SSE ist einfacher (kein Bidi, einfaches HTTP), WebSocket flexibler. Für reine Server-Push ohne Client-Heartbeat reicht SSE. Empfehlung: SSE für `/v1/quotes/stream` und `/v1/events/stream`. Reconnect mit `Last-Event-ID`-Header.
- **Token-Format: JWT oder Opaque?** JWT erlaubt stateless-Validation (gut für Skalierung), Opaque erlaubt einfaches Revoke (gut für Sicherheit). Empfehlung: Opaque mit Redis-Lookup oder einfacher JSON-Datei, weil `broker-gateway` ohnehin Singular läuft.
- **Idempotency-Storage: Memory, Redis, SQLite?** Memory reicht solange ein einziger Service-Prozess läuft. Restart wäscht alle Idempotency-Keys aus, was OK ist (Consumer können auf TTL verlassen oder neuen Key generieren). Empfehlung: Memory mit konfigurierbarer TTL.
- **Reverse-Proxy / TLS?** Aktuell: lokal HTTP, vor TLS würde nginx/Caddy davor. Frage: brauchen wir öffentlichen Endpoint (dann externer Proxy + Let's Encrypt) oder bleibt es Tailscale/internal-only?
- **Compose vs. Standalone-Container?** Compose mit gateway+cpgateway als zwei Services im selben Stack. Standalone wäre auch möglich, aber dann muss CP-Gateway anders gemanagt werden. Empfehlung: Compose, analog zu PSM.

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

## 10. Aktueller Zustand (Bootstrap-Snapshot)

- Repo: angelegt, public, leer bis auf README + CLAUDE.md + .gitignore.
- KanPrompt-Projekt: angelegt, ID `a6a45428-ac37-48f5-b295-d3ff26f31711`, Farbe green.
- AP-01: dieses Arbeitspaket, ID `411c3b19-7737-4a50-a9ff-5c521a302372`.
- Karten: noch keine. Werden auf Basis dieses Dokuments durch Cowork erstellt.
- Implementierung: noch keine.
- Deploy-Target: cma-pi-1, Pfad noch zu wählen (Vorschlag `/mnt/ssd/broker-gateway`).

---

*Version 1.0 — Bootstrap-Kontext, 2026-04-24*
