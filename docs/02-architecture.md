# 02 — Architektur

Lebendes Architektur-Dokument für `broker-gateway`. Single Source of Truth
für die Frage: **Wie ist dieser Service gebaut, und warum so?**

> **Abgrenzung:** dieses Dokument beschreibt Aufbau und Prinzipien. Die
> konkrete API-Form (Endpunkte, Bodies, Status-Codes, Error-Modell) lebt
> in `docs/api/v1.md`. Operationelle Anleitungen (Login, Deploy,
> Troubleshooting) liegen unter `docs/runbooks/`. Die historische
> Bootstrap-Session ist in `docs/01-context-from-bootstrap-session.md`
> festgehalten — wer Architektur-Inhalte sucht, findet sie hier, nicht
> dort. **Begriffsklärungen:** [`docs/06-glossary.md`](06-glossary.md).

**Stand:** v1.34.1, 2026-05-09 (Snapshot zum Architektur-IST nach
AP-01..AP-11 plus TWS-Refactor Karten 368ccdfe / 8b1781d3 / 441b53db /
33cb35b1; das v1.32–v1.34-Update führt das TWS-Backend parallel zum
cpgateway-Pfad ein, gewählt per `BG_BACKEND=cp|tws`).

## Inhalt

1. [Zweck und Abgrenzung](#1-zweck-und-abgrenzung)
2. [Kontext — warum existiert dieser Service](#2-kontext--warum-existiert-dieser-service)
3. [Architektur-Prinzipien](#3-architektur-prinzipien)
4. [Komponenten und Container-Komposition](#4-komponenten-und-container-komposition)
5. [IBKR-Adaptions-Schicht](#5-ibkr-adaptions-schicht)
6. [Auth-Modell](#6-auth-modell)
7. [Streaming-Architektur](#7-streaming-architektur)
8. [Logging-Architektur](#8-logging-architektur)
9. [Test-Strategie](#9-test-strategie)
10. [Was bewusst NICHT in v1 ist](#10-was-bewusst-nicht-in-v1-ist)
11. [Verweise und offene Fragen](#11-verweise-und-offene-fragen)

---

## 1. Zweck und Abgrenzung

`broker-gateway` ist ein **Singular-Service**, der die IBKR-Trading-Session
als gemultiplexte HTTP-API ausliefert. Consumer (PSM, trading-robot,
Ad-hoc-CLI/Notebooks) sprechen ausschließlich über `/v1` mit dem
Gateway, sehen IBKR niemals direkt.

| Frage | Lebt in |
|---|---|
| Wie ist der Service intern gebaut? | **Dieses Dokument** |
| Welche Endpunkte gibt es, mit welchen Bodies/Headern? | [`docs/api/v1.md`](api/v1.md) |
| Wie deploye ich auf cma-pi-1? | [`docs/runbooks/cpgateway-login.md`](runbooks/cpgateway-login.md), [`docs/cp-recordings.md`](cp-recordings.md), README |
| Was ist beim Bootstrap entschieden worden? | [`docs/01-context-from-bootstrap-session.md`](01-context-from-bootstrap-session.md) |
| Welches IBKR-CP-API-Detail liegt hinter Feld X? | [`docs/research/`](research/) |
| Welche WebSocket-Topics liefern was? | [`docs/research/ibkr-cpapi-websockets-findings.md`](research/ibkr-cpapi-websockets-findings.md) |

Architektur-Aussagen, die hier stehen, sollen **nirgendwo sonst** dupliziert
sein. README enthält Quickstart und Verweise; CLAUDE.md verweist auf
dieses Dokument als Lese-Pflicht für neue Sessions.

---

## 2. Kontext — warum existiert dieser Service

### 2.1 IBKR Single-Session-Constraint

IBKR erlaubt **eine einzige Trading-Session pro Konto**, nicht pro
Benutzer. Sobald zwei Komponenten parallel mit demselben Konto am
IBKR-CP-Gateway hängen würden, kickt sich das eine das andere weg.
Das ist in der Bootstrap-Session am 2026-04-23 reproduziert worden:
ein Browser-Login ins IBKR-Kundenportal hat die laufende
CP-Gateway-Session invalidiert (HTTP 401 nach Re-Login).

**Konsequenz:** Es muss genau eine Komponente geben, die exklusiv die
IBKR-Session hält. Alle anderen müssen über sie sprechen.

### 2.2 Rate-Limit-Realität

IBKR throttelt pro Konto auf grob **50 Nachrichten/Sekunde** über alle
gleichzeitigen Verbindungen, mit zusätzlich endpoint-spezifischen
Limits (Snapshot anders als Orders anders als Subscriptions).
Mehrere unkoordinierte Caller produzieren `Pacing violation` und
Verbindungsabbrüche.

**Konsequenz:** Eine zentrale Throttle-Schicht serialisiert Requests.
Nur sie weiß, wie viele Requests aktuell offen sind.

### 2.3 Subscription-State ist global pro Session

Marktdaten-Subscriptions im CP Gateway sind **session-global**, nicht
caller-spezifisch. Wenn Caller A `AAPL` abonniert und Caller B den
unsubscribt, verliert A unbemerkt seinen Stream. Es braucht
**Refcounting**: ein Symbol bleibt subscribed solange mindestens ein
Consumer es will.

### 2.4 Auth- und Tickle-Lifecycle

CP Gateway braucht alle ~60 s einen `POST /tickle`, sonst läuft die
Session aus. Browser-Login muss vor Ablauf erneuert oder per
`reauthenticate` aufgefrischt werden. Das ist nicht-trivialer Zustand,
den nur eine Stelle halten darf.

### 2.5 IP-basierte Session-Whitelist (Karte 739777a9)

cpgateway bindet eine Login-Session an die **TCP-Source-IP**, von der
der Browser-Submit kam. Folge-Requests von einer anderen Source-IP
bekommen HTTP 401, auch mit korrekten Cookies. Aus der IBKR-Doku:
„authentication must be done on the same machine where the gateway is
running". IBeam-README hält dasselbe Constraint fest („bridge network
mode required due to clientportal.gw IP whitelist").

Konsequenz im Compose-Stack:

- Pi-Desktop-Browser auf `127.0.0.1:5001` → Source-IP für cpgateway
  ist die Bridge-Gateway-IP (z. B. `172.23.0.1`).
- `broker-gateway-paper`-Service → eigene Bridge-IP (z. B.
  `172.23.0.3`).
- cpgateway sieht zwei verschiedene Sessions; die zweite ist nicht
  authenticated.

Lösungspfad: **Login-Browser im selben Network-Namespace wie der
Service** betreiben. Praktisch entweder
`docker run --network=container:broker-gateway-paper <login-sidecar>`
oder `nsenter --net=/proc/<service-pid>/ns/net chromium ...` (Pi-
Desktop-Chromium ins Service-netns einklinken — Display bleibt
wayvnc, TCP-Stack ist Service). Helper-Skript:
[`ops/cp-login-pi-nsenter.sh`](../ops/cp-login-pi-nsenter.sh).

Headless-Auto-Login aus dem Container heraus scheitert unabhängig von
der Browser-Engine am Live/Paper-Toggle: das IBKR-Login-Form-Bundle
prüft auf trusted Mouse-Events, der State-Change kommt nicht durch
zum Server. Sowohl Playwright + Firefox als auch Selenium + Chrome
(IBeam) reproduzieren das (Phase 2.1c und 2.1e in Karte 739777a9).
Manueller User-Click via wayvnc bleibt der einzige zuverlässige Pfad.

### 2.6 Bewährte IBKR-Beobachtungen

In der Bootstrap-Session und in den Live-Recordings 2026-04-23..2026-04-30
mehrfach reproduziert:

- **First-Call-Primes-Subscription:** der erste Snapshot-Call liefert
  `[{conidEx, conid}]` ohne Werte. Erst der zweite Call (~3 s später)
  liefert reale Daten.
- **6509-Availability-Code:** drei Zeichen (`DPB` = Delayed/Paid/Book,
  `RPB` = Realtime/Paid/Book). Entscheidet Realtime vs. Delayed
  unabhängig vom Portal-„aktiv"-Listing.
- **Konto-Reifung:** Non-Pro-US-Realtime-Streams werden im Portal als
  „aktiv" gelistet, sind aber erst nach 30 Tagen Kontoalter oder
  Erreichen der USD-30-MTD-Commission-Waiver tatsächlich freigeschaltet.
- **whatif-Risk-Subsystem** prüft ein anderes Marktdaten-Flag als der
  Snapshot-Endpoint. Ohne Realtime-Freigabe liefert whatif Warnings 4 +
  21 („Percentage price check cannot be performed", „blind trading"),
  selbst wenn der Snapshot-Endpoint brauchbare Werte liefert.
- **MTD-Commissions** lassen sich aus `/iserver/account/trades?days=30`
  aggregieren (Summe `commission`-Feld), aber das Feld hat keine
  Währungsangabe — bei mehrheitlich US-Aktien plausibel USD.

---

## 3. Architektur-Prinzipien

Verbindlich — neue Karten dürfen sie nicht eigenmächtig brechen.

### 3.1 Singular-Halter

Es gibt **genau eine** Instanz von `broker-gateway` pro IBKR-Konto.
Das ist keine Skalierungs-Entscheidung, sondern Hard-Constraint von
IBKR (siehe 2.1). Skalierung passiert auf Consumer-Seite, nicht hier.

Die **Konto-Identität hinter dem Singular-Halter ist austauschbar**:
das Prinzip "eine Instanz pro Konto" bleibt unverändert, wenn die
broker-gateway-Live-Instanz von Konto A auf Konto B umgezogen wird.
Heute ist die Live-Instanz an U25235077 (Privatkonto des Operators)
gebunden; ein Cutover auf ein dediziertes Service-Konto ist seit
2026-05-18 in Vorbereitung — Operator-Pfad in
[`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md),
Status in Sektion 11.2 ("Account-Identitaet-Wechsel"). Motivation:
jeder Operator-Login auf U25235077 im Browser oder in der IBKR-App
kollidiert mit der broker-gateway-Live-Session (Single-Session-
Constraint 2.1); das Service-Konto entkoppelt das.

### 3.2 Stateless-Außen, Stateful-Innen

Nach außen verhält sich `/v1` wie ein klassischer REST-Service: jeder
Request ist self-contained (Bearer-Token im Header, alle nötigen
Parameter im Body/Query). Innen hält der Service Auth-Session,
Subscription-State, Idempotency-Map, Order-Cache, Throttle-Buckets.
Diese Trennung erlaubt minimale Consumer.

### 3.3 Versioniert am Contract, nicht am Code

`/v1` muss rückwärtskompatibel bleiben, solange er angeboten wird.
Additive Felder in Responses sind erlaubt. Breaking Changes
ausschließlich in `/v2`. `/v1` und `/v2` können parallel laufen, mit
`Deprecation`-Header bei Ablauf-Plan.

### 3.4 Idempotency für Schreiboperationen

Jede Schreiboperation (Order, Cancel) erfordert einen
`Idempotency-Key`-Header. Der Service speichert das Mapping
`key -> response` für eine konfigurierbare TTL (Default 24 h).
Wiederholungen liefern identische Responses ohne erneuten Broker-Call.
**Schutz vor Duplicate-Orders bei Netzwerk-Retries.**

Implementierung: `src/broker_gateway/idempotency.py`, In-Memory-Map.

### 3.5 Transient

Kein Business-Persistenz-Layer. Session-State, Cache, Subscription-Map,
Idempotency-Map liegen im Prozess-Memory. Optional Redis als externes
State-Backing für Restart-Persistenz, aber **kein eigenes Schema, kein
Alembic**. Datenmodell des Services ist die API selbst.

### 3.6 Single Source of Truth pro Concern

| Concern | SSOT |
|---|---|
| API-Contract | `docs/api/v1.md` |
| Scopes | `src/broker_gateway/auth/models.py` |
| Header-Redaktion | `src/broker_gateway/cp/redaction.py` |
| Mock-Fixtures | `tests/fixtures/recorded/` (live > seed) |
| Architektur-Beschreibung | dieses Dokument |

Wo etwas dupliziert wird, gewinnt der SSOT. Verstöße werden in einer
Folge-Karte behoben, nicht im laufenden Code „mal eben angepasst".

### 3.7 Observability eingebaut

- **Strukturierte Logs:** JSON-Lines, ein Event pro HTTP-Request inkl.
  `request_id`, `caller_id`, `scope`, `latency_ms`. Drei Stränge
  (siehe Sektion 8).
- **Prometheus-Metriken** unter `/metrics` (kein `/v1`-Prefix):
  Request-Count/Latency pro Endpoint, IBKR-Session-Age,
  Subscription-Count, Pacing-Violations, Throttle-Extra-Wait.
- **Healthcheck mit Failure-Modes:** `/v1/health` unprivilegiert,
  `/v1/internal/health` admin-geschützt mit IBKR-Detail
  (`auth_lost`, `ibkr_down`, OK).

---

## 4. Komponenten und Container-Komposition

### 4.1 Compose-Stack

Der Service läuft als Docker-Compose-Stack. Es gibt zwei Backend-Pfade,
gewählt per `BG_BACKEND` (Default `cp`):

```
+---------------------------+
| Consumer (PSM, robot, …)  |
|        HTTP / SSE         |
+-------------+-------------+
              | Port 4000 (extern)
              v
+---------------------------+
| gateway                   |
| broker-gateway:1.34.1     |
| Port 8000 intern          |
| (FastAPI / uvicorn)       |
+----+--------+-------------+
     |        |
     | (cp)   | (tws)         BG_BACKEND wählt zur Lifespan-Zeit
     v        v
+----+----+ +-+-----------+
| cp-     | | tws         |
| gateway | | (IB Gateway |
| :5000   | |  + IBC)     |
| (Java)  | | :4001/4002  |
+----+----+ +-+-----------+
     |        |
     | HTTPS  | TWS-Socket
     v        v
       api.ibkr.com (IBKR Backend)
```

- **`gateway`** (Image `broker-gateway:1.34.1`): die FastAPI-App,
  intern Port 8000, extern auf 4000 publiziert.
- **`cpgateway`** (Image `broker-cpgateway:1.0.3`): IBKR Client Portal
  Gateway als Java-Prozess (eclipse-temurin), nur intern. Externer
  Zugriff für den Browser-2FA-Login ausschließlich über SSH-Reverse-Tunnel
  bzw. Pi-Desktop-Browser auf `127.0.0.1:5000` (Live) / `:5001` (Paper);
  siehe [`docs/runbooks/cpgateway-pi-desktop-login.md`](runbooks/cpgateway-pi-desktop-login.md).
- **`tws`** (geplanter Compose-Service, Container-Slot in Karte 8b1781d3
  vorbereitet): IB Gateway + IBC, Default-Image `gnzsnz/ib-gateway:stable`.
  Heute noch nicht im `compose.yaml` aktiviert — Karte 6 (Hard-Cutover)
  zieht den Compose-Eintrag nach. Solange läuft TWS-Verkehr in Tests
  und Spike-Setups gegen einen lokal gestarteten IB-Gateway-Container.
- `gateway` wartet via `depends_on: condition: service_healthy` auf den
  `cpgateway`-Healthcheck. Im Hard-Cutover-Endzustand ersetzt der
  `tws`-Healthcheck diese Bedingung.
- Beim Image-Bump muss der `image:`-Tag in `compose.yaml` mitgezogen
  werden (Konvention: Image-Tag = Service-Version).

Deployment-Target: cma-pi-1 unter `/mnt/ssd/broker-gateway` (Live) bzw.
`/mnt/ssd/broker-gateway-paper` (Paper). Gateway-Port 4000/4001 (Live/
Paper) ist frei (KanPrompt belegt 8000/8001 auf demselben Host).

### 4.2 Repo-Layout

```
src/broker_gateway/
  api/v1/                # FastAPI-Router pro Endpoint-Gruppe
    health.py            # /v1/health, /v1/internal/health
    auth.py              # /v1/auth/token (POST/DELETE)
    instruments.py       # /v1/instruments/*
    quotes.py            # /v1/quotes/snapshot
    quotes_stream.py     # /v1/quotes/stream (SSE)
    portfolio.py         # /v1/portfolio/{accountId}/*
    orders.py            # /v1/orders, /v1/orders/{id}
    trades.py            # /v1/trades
    events_stream.py     # /v1/events/stream (SSE)
    errors.py            # zentrale Error-Envelope-Helper
  auth/                  # Token-Modell, Store, FastAPI-Middleware
  auth_status.py         # AuthStatus-Enum (SSOT, sechs Werte) +
                         #   to_consumer_status + is_session_unavailable
  cp/                    # IBKR-Adapter-Schicht ueber CP Gateway (Sektion 5)
    client.py            # CPGatewayClient (httpx-basiert)
    lifecycle.py         # Auth-Status, Tickle-Job, Reauthenticate;
                         #   re-exportiert AuthStatus aus auth_status.py
    redaction.py         # Header-Redaktion (SSOT)
    recorder.py          # httpx-Event-Hook fuer Live-Recordings
    normalize.py         # Money / Availability / Currency
    instruments.py       # conid-Cache + Search-Adapter
    quotes.py            # Snapshot mit First-Call-Prime
    portfolio.py         # Summary / Positions / Ledger
    orders.py            # Order-Lifecycle (whatif, reply-loop)
    trades.py            # Trades-History + MTD-Aggregation
    ws_client.py         # CPWebSocketClient (AP-04 K3)
    auto_login_trigger.py / auto_login_throttle.py  # Auto-Login-Pfad
                                                    # (cp-spezifisch, Sektion 6.4)
  tws/                   # IBKR-Adapter-Schicht ueber TWS-Socket (Karten
                         #   441b53db + 33cb35b1, Sektion 5.1)
    client.py            # TWSClient (ib_async-basiert), ClientIdPool
    lifecycle.py         # TWSLifecycle (Heartbeat) + TWSLifecycleCpAdapter
                         #   (cp-Slot-Kompatibilitaet)
  streams/               # SSE-Stream-Manager (Quotes + EventBus)
    manager.py           # Subscription-Refcount + Fan-Out
    events.py            # EventBus fuer /v1/events/stream
  throttle/              # Token-Bucket (Pro-Endpoint-Klasse)
  middleware/observability.py  # request_id, structlog binding, metrics
  metrics.py             # Prometheus-Collectoren
  cache.py               # generischer TTL-Cache
  money.py, availability.py    # Pure-Funktionen fuer Normalisierung
  idempotency.py         # In-Memory Idempotency-Key-Map
  logging_setup.py       # structlog + Routing auf drei Straenge
  main.py                # FastAPI-App-Factory + Router-Wiring
```

Kein eigenes Repo-Layout für Consumer — Consumer hängen sich nur an
`/v1`.

### 4.3 Healthchecks und Failure-Modes

| Endpoint | Auth | Liefert | Wozu |
|---|---|---|---|
| `GET /v1/health` | keiner | `{"status":"ok","version":"1.34.1"}` | Liveness ohne IBKR-Abhängigkeit |
| `GET /v1/internal/health` | `admin:*` | Roher Backend-Status (`auth_status`) plus stabiler `auth_status_consumer` (`ok`/`down`/`lost`), Heartbeat-Age, Subscription-Count | Readiness mit Detail; Schema gleich für CP- und TWS-Backend |
| `GET /v1/internal/tws-health` | `admin:*` | TWSClient-spezifische Diagnose (Connect-State, ClientId, Last-Heartbeat); nur sinnvoll unter `BG_BACKEND=tws` (Karte 441b53db) | TWS-Backend-Diagnose |
| `GET /metrics` | keiner (nur intern) | Prometheus-Format | Scrape vom Pi-Prometheus |

Bei Session-Verlust antworten alle Business-Endpunkte mit `503` und
`Retry-After: 30`. Im CP-Pfad läuft der Recovery-Mechanismus im
Hintergrund (bis zu drei `reauthenticate`-Versuche, dann 503 bis
Operator-Eingriff bzw. Auto-Login-Sidecar im Paper-Stack). Im TWS-Pfad
übernimmt IB Gateway + IBC den Daily-Restart und Sat-Reset; broker-
gateway reconnected automatisch beim nächsten Heartbeat.

---

## 5. IBKR-Adaptions-Schicht

Die `cp/`- und `tws/`-Module sind die einzigen Stellen, an denen IBKR-
Spezifika adressiert werden. Außerhalb dieser Pakete darf nichts
wissen, dass hinter dem Gateway IBKR steht. Seit v2.0.0 ist die
`tws/`-Familie der Default-Pfad: sie spricht IB Gateway + IBC über
die TWS-Socket-API (`ib_async`) — siehe Sektion 5.1. Die `cp/`-Familie
spricht das interne Client Portal Gateway über HTTP/REST an und ist
seit v2.0.0 nur noch unter Compose-Profile `cp-legacy` aktiv (Notfall-
Roll-Back). Auswahl per `BG_BACKEND=tws|cp`.

**Schema-Garantie:** Beide Adapter-Familien teilen sich die
Pydantic-Modelle. Die `tws/`-Module importieren `Position`, `Ledger`,
`Quote`, `Trade`, `Instrument`, `ExchangeCalendar` etc. direkt aus
`cp/` (Single-Source-of-Truth), und `tws/orders.py` nutzt die
Backend-übergreifenden Modelle aus `broker_gateway.order_models`.
Damit ist der HTTP-API-Vertrag (`/v1/...`) zwischen den Backends
strukturell identisch — die Garantie wird in
`tests/test_tws/test_schema_compat.py` (AP `2a203c58` Phase 7)
explizit verriegelt.

| IBKR-Eigenheit | Lösung im Gateway | Modul |
|---|---|---|
| First-Call leerer Snapshot | Server primt intern: bei Snapshot-Anfrage zwei Calls, gibt nur den zweiten zurück. Consumer sieht immer Daten. | `cp/quotes.py` |
| Tickle alle 60 s | Hintergrund-Job (`asyncio.Task`) solange Auth aktiv. | `cp/lifecycle.py` |
| Browser-2FA-Login initial | Operator-Aufgabe (Runbook). Service erkennt invalide Session und meldet `auth_lost` über Internal-Health; Consumer-API liefert 503 + `Retry-After`. | `cp/lifecycle.py` |
| Session-Kicked durch Portal-Login | Service erkennt es im Tickle-Lifecycle, versucht bis zu 3x `reauthenticate`, sonst 503. | `cp/lifecycle.py` |
| Subscription-Limits (~5 conids/Snapshot, ~250 Streams/Session) | Subscription-Manager mit Refcount + Multi-Snapshot-Aggregation; Throttle pro Endpoint-Klasse. | `streams/manager.py`, `throttle/` |
| Symbol/conid-Mapping | TTL-Cache, Symbole wechseln conid praktisch nie. | `cp/instruments.py`, `cache.py` |
| Currency in Order/Portfolio-Bodies | Explizit normalisieren: jedes Geldfeld bekommt `value` + `currency`. | `cp/normalize.py`, `money.py` |
| 6509-Availability-Code | Im Quote-Response in eigenem Feld `availability` mit semantischer Übersetzung (`realtime` / `delayed` / `frozen`). Roher Code zusätzlich für Debug. | `availability.py`, `cp/quotes.py` |
| Order-Confirmation-Replies | IBKR fragt vor Order-Submit bis zu 3 Confirmations (price-cap, no-market-data, mandatory-cap). Adapter loopt automatisch über `/iserver/reply/{id}` mit `confirmed:true`. | `cp/orders.py` |
| Trade-Aggregation MTD | `/iserver/account/trades?days=30` summieren, ohne Currency-Annotation (Empirie: USD bei US-Aktien). | `cp/trades.py` |

Live-Snapshot-Lookup geht durch `CPGatewayClient` (`cp/client.py`),
ein httpx-AsyncClient mit Recording-Event-Hook (s. Sektion 9).

### 5.1 TWS-Backend-Adapter (Default seit v2.0.0)

Seit v1.34.0 (Karte 33cb35b1) gibt es das `tws/`-Backend, das die
IBKR-Trading-Session über die TWS-Socket-API (`ib_async` + IB Gateway
+ IBC) ausliefert. Mit v2.0.0 (Karte 5) wurde es zum Default-Pfad,
und mit AP `2a203c58` (Phasen 1–7, v2.0.5–2.1.0, Mai 2026) wurden
alle Daten-Adapter (Portfolio, Instruments, Quotes, Orders, Trades,
Calendar) auf `ib_async` umgezogen. Die Wahl erfolgt über das
ENV-Flag `BG_BACKEND`:

| `BG_BACKEND` | Lifecycle | Snapshot-Quelle | Status-Werte |
|---|---|---|---|
| `tws` (Default) | `tws/lifecycle.py::TWSLifecycle` | Heartbeat alle `BG_TWS_HEARTBEAT_SEC` (Default 60 s) über `ib.isConnected()` + `ib.client.isReady()` | `ok`, `session_lost`, `tws_down` |
| `cp` (Profile cp-legacy) | `cp/lifecycle.py::AuthLifecycle` | Tickle-Loop alle 60 s + SSO-Validate + iserver-Bridge-Probe | `ok`, `reauth_pending`, `auth_lost`, `cp_down` |

Der TWS-Lifecycle hat **keinen Reauth-Mechanismus** — IB Gateway + IBC
übernehmen Daily-Restart und Saturday-Reset selbständig. Aus dem
Lifecycle-Heartbeat ergibt sich die State-Maschine pro Tick:

```
isConnected? --no--> try connect --+--> success -> ready? --yes--> OK
                                    |                       --no---> SESSION_LOST
                                    +--> fail x3 -> TWS_DOWN
isConnected? --yes-> ready? --yes--> OK
                     ready? --no---> SESSION_LOST
```

Die zentrale Quelle für beide Status-Räume ist das Enum
`broker_gateway.auth_status.AuthStatus` (sechs Werte). `cp/lifecycle.
py` re-exportiert es für Backward-Compat. Konsumenten-Code, der
backend-unabhängig sein will, nutzt die Mapping-Funktion
`to_consumer_status` (`ok | down | lost`); `/v1/internal/health`
rendert sowohl den feinen `auth_status` als auch den stabilen
`auth_status_consumer`-View.

Die Lifecycle-Auswahl läuft in `main.py::lifespan`. Im `BG_BACKEND=tws`-
Pfad wird ein `TWSLifecycleCpAdapter` unter `app.state.cp_lifecycle`
gehängt, der einen `cp.lifecycle.LifecycleSnapshot` mit cp-kompatiblen
Feldern liefert (cp-spezifische Felder wie `last_sso_validate_at`,
`iserver_bridge_ok`, `last_auto_login_*` bleiben `None`). Damit greifen
alle bestehenden Endpunkte (`require_session_ok`, `/v1/internal/
health`, `/v1/status`) ohne Refactor — ein Hard-Cutover des cp-Pfads
ist Migration-Karte 6.

**Service-Schicht-Status (Stand AP `2a203c58` Phase 7, v2.1.0):**

Alle Daten-Adapter sind im `BG_BACKEND=tws`-Pfad nativ über `ib_async`
implementiert und werden im Lifespan über die `app.state.<service>`-
Felder verdrahtet:

| Endpoint-Familie | TWS-Service | cp-Service (Profile cp-legacy) |
|---|---|---|
| Portfolio | `tws.portfolio.TWSPortfolioService` | `cp.portfolio.PortfolioService` |
| Instruments | `tws.instruments.TWSInstrumentsService` | `cp.instruments.InstrumentsService` |
| Quotes (Snapshot + Stream) | `tws.quotes.TWSQuotesService` (auch Stream-Quelle) | `cp.quotes.QuotesService` + `streams.manager.SubscriptionManager` |
| Orders + Trades | `tws.orders.TWSOrdersService`, `tws.trades.TWSTradesService`, `tws.orders.TWSOrdersBootstrapLoader`, `tws.orders.TWSOrdersStreamPump` | `cp.orders.OrdersService`, `cp.trades.TradesService`, `api.v1.orders_stream.OrdersBootstrapLoader` |
| Calendar / Exchanges | `tws.calendar.TWSCalendarService` (Static-Mapping) | `cp.calendar.CalendarService` |

**Hard-Guards (AP `2a203c58` Phase 6):**
- `app.state.backend` führt den aktiven Backend-String (`"cp"`/`"tws"`)
  als Single-Source-of-Truth.
- `app.state.cp_client` existiert **nur** im cp-Mode. Im tws-Mode
  bekommen Konsumenten beim Zugriff einen `AttributeError` — bewusst
  laute Fehlersignatur statt blinder Call gegen einen abgeschalteten
  cpgateway-Container.
- `POST /v1/internal/seed-cookies` antwortet im tws-Mode mit HTTP 503
  + `not_applicable_in_tws_mode`, weil es im tws-Pfad keinen
  Browser-Login zu seeden gibt.

---

## 6. Auth-Modell

Single Source of Truth für Scopes: `src/broker_gateway/auth/models.py`.
Tokens sind opake Strings (kein JWT) — passt zur Singular-Natur des
Services und ermöglicht jederzeit Revoke ohne Verifikations-Cache.

### 6.1 Bootstrap

Beim Start liest die App `BG_BOOTSTRAP_ADMIN_TOKEN` aus dem Environment.
Wenn gesetzt, wird der Wert als Admin-Token mit Scope `admin:*`
registriert. Persistenz wahlweise über `BG_TOKEN_FILE`
(`/var/lib/broker-gateway/tokens.json`, atomare Writes); ohne
Variable arbeitet der Service mit In-Memory-Store.

### 6.2 Scopes

| Scope | Berechtigt zu |
|---|---|
| `instruments:read` | Symbol- und conid-Lookup |
| `quotes:read` | Snapshots + Streams |
| `portfolio:read` | Portfolio + Positions + Ledger + Trades |
| `orders:write` | Orders platzieren / canceln + Order-Status |
| `events:read` | Events-Stream |
| `admin:*` | Token-Verwaltung; passt automatisch alle Scope-Checks |

Standard-Mapping pro Consumer:

| Consumer | Erwartete Scopes |
|---|---|
| **personal_stock_manager (PSM)** | `quotes:read`, `portfolio:read`, `instruments:read` (kein `orders:write`) |
| **trading-robot** | `quotes:read`, `portfolio:read`, `instruments:read`, `orders:write`, `events:read` |
| Admin-CLI / Notebooks | konfigurierbar, mit Rotation, kurzlebig |

### 6.3 Lifecycle

- Token erzeugen: `POST /v1/auth/token` mit Admin-Token im Authorization-Header.
- Token revoken: `DELETE /v1/auth/token` (Self oder `admin:*`).
- Auth-Middleware (`auth/middleware.py`) prüft Bearer-Token gegen
  `Store` (Memory oder File-backed), bindet `caller_id` und `scopes`
  per `structlog.contextvars` an den Request.

Token-Werte werden **nirgendwo geloggt** — nur `caller_id` und
`scopes`. Die Header-Redaktion (s. Sektion 8) entfernt
`Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, `X-Auth-Token`
und `Proxy-Authorization` aus jedem Log-Strang.

### 6.4 Stack-Kennung und Auto-Login (Karte ece90a8e)

> **Backend-Hinweis:** Der Auto-Login-Pfad ist cp-spezifisch — er heilt
> den Container-Recreate-Verlust der **cpgateway-Session**. Im
> `BG_BACKEND=tws`-Pfad existiert kein Pendant, weil der TWS-Lifecycle
> ohne Browser-2FA läuft (IBC + IB Gateway machen Saturday-Reset und
> Daily-Restart selbst). `_maybe_attach_auto_login` in `main.py` skipped
> entsprechend, wenn der gewählte Lifecycle keine `AuthLifecycle`-
> Instanz ist (ab v1.34.0).

Seit v1.28.0 ist der Service stack-aware: jede Instanz kennt sich
selbst als **Live**- oder **Paper**-Stack über `BG_STACK_KIND` (Pflicht-
Env). Der Wert wird in `config.py::stack_kind()` gelesen und beim
Lifespan-Start gegen ein Hard-Guard-Set geprüft (`validate_runtime_
config`):

1. **Hard-Guard 1 (App-Level):** `BG_STACK_KIND=live` UND
   `BG_PAPER_AUTO_LOGIN=1` → `ConfigError` beim Lifespan-Start. Der
   Service kommt gar nicht hoch, kein einziger Tickle wird abgesetzt.
2. **Hard-Guard 1b (App-Level):** `BG_STACK_KIND=live` UND
   `BG_PAPER_USERNAME`/`BG_PAPER_PASSWORD` gesetzt → ebenfalls
   `ConfigError`. Damit kann eine versehentlich auf den Live-Stack
   gemountete `.env.paper` nicht stillschweigend Credentials in einen
   Live-Container schreiben — der Fail kommt sofort beim Start.
3. **Hard-Guard 2 (Sidecar):** Phase-B-Sidecar prüft selbst, dass die
   Ziel-URL `paper-cpgateway` enthält und exitet sonst mit Code 5,
   bevor er die Form rendert.
4. **Hard-Guard 3 (Compose):** Live-Compose hat **keinen**
   `docker.sock`-Mount und **keine** `BG_PAPER_*`-Variablen. Phase A
   etabliert die Trennung über `ops/build-gateway.sh` (`BG_STACK_KIND`
   wird per `--env=…`-Schalter exportiert) und die getrennten
   `.env.live.template` / `.env.paper.template`. Phase B fügt im
   Paper-Compose ein Override für den Sidecar-Service hinzu.

**Auto-Login-Pfad (Phase A skeleton, Phase B vollständig):**

```
  paper-cpgateway killed (Container-Recreate beim Deploy)
        │
        ▼
  AuthLifecycle._heartbeat_sso → CP_DOWN
        │
        ▼
  AutoLoginTrigger.maybe_trigger
   ├─ enabled? (BG_PAPER_AUTO_LOGIN=1)
   ├─ stack_kind == "paper"?
   ├─ auth_status in {auth_lost, cp_down}?
   ├─ AutoLoginThrottle.attempt() erlaubt?
   │   └─ blockt nach Limits: 5min/15min/45min Backoff,
   │      max 3/h, max 5/Tag, "2fa_required_manual_intervention"
   │      sticky bis Service-Restart
   ▼
  AutoLoginRunner.run()  ← Phase B: docker run --rm Sidecar
        │                    Phase A: Mock-Runner in Tests
        ▼
  AutoLoginResult{exit_code, duration_s, error}
        │
        ▼
  AuthLifecycle.update_auto_login(...)
   → Felder im LifecycleSnapshot:
     last_auto_login_attempt_at, last_auto_login_success_at,
     auto_login_failures_total, auto_login_throttle_state
   → sichtbar in /v1/internal/health
```

`AutoLoginTrigger`, `AutoLoginThrottle` und `AutoLoginResult` leben in
`src/broker_gateway/cp/auto_login_trigger.py` bzw.
`auto_login_throttle.py`. Phase A liefert das Skeleton + Tests; der
echte docker-SDK-`Runner` und das Sidecar-Image (`ops/auto-login/`)
folgen in Phase B.

**Reichweite:** Auto-Login heilt das Container-Recreate-Problem
(Session-Memory weg) automatisch. Es heilt **NICHT** den Fall, dass
IBKR die Session aus eigenen Gründen kappt (Wartungsfenster,
Account-Lock, 2FA-Policy-Änderung) — dort wird zuerst der Reauth-Loop
versucht, und falls auch der scheitert, fällt der Pfad auf
Auto-Login zurück. Bei IBKR-2FA-Policy-Änderung exitet der Sidecar
mit Code 4, der Throttle-Status springt auf
`2fa_required_manual_intervention`, und jeder weitere Trigger wird
gestoppt — bis ein Mensch den Service neu startet.

---

## 7. Streaming-Architektur

### 7.1 Heute: SSE für Quotes und Events (v1.11.0)

Zwei SSE-Endpunkte sind in Produktion:

| Endpunkt | Modul | Reconnect | Inhalt |
|---|---|---|---|
| `GET /v1/quotes/stream` | `streams/manager.py`, `api/v1/quotes_stream.py` | Client-seitiges Reconnect via `Last-Event-ID` | Quote-Updates pro Symbol |
| `GET /v1/events/stream` | `streams/events.py`, `api/v1/events_stream.py` | dito | Execution-Reports, Position-Updates, Status-Changes |

Auf der IBKR-Seite gibt es **eine** Subscription pro `conid`. Mehrere
Consumer für dasselbe Symbol bekommen den Fan-Out aus einer einzigen
IBKR-Subscription. Der `SubscriptionManager` hält Refcounts und
unsubscribed erst, wenn der letzte Consumer den Stream verlässt.

`request_id` und `caller_id` werden per
`structlog.contextvars.bind_contextvars` gesetzt; SSE-Bodies
materialisieren wir nicht (`response_streaming: true` im Inbound-Log).

### 7.2 Discovery für WebSocket (AP-04, IST: K1..K4 done)

Phase 1 von AP-04 hat den IBKR-CP-Gateway-WebSocket
(`wss://localhost:5000/v1/api/ws`) explorativ vermessen. Die
Findings sind in `docs/research/ibkr-cpapi-websockets-findings.md`
mit Topic-Reife-Ranking konsolidiert:

| Topic | Reife (K4 Live-Test U25235077, heute aktiver Live-Account — Cutover auf dediziertes Service-Konto geplant, siehe Sektion 11.2) | Bemerkung |
|---|---|---|
| `smd` (Marktdaten) | grün | Subscribe-Format `smd+<conid>+{...}`, Felder als Deltas, mixed-type values |
| `sor` (Order-Updates) | grün | volle Order-Lifecycle-Frames, kompakter als REST-Polling |
| `str` (Trade-Stream) | gelb | Frequency hoch, Inhalt überprüfungsbedürftig |
| Reconnect-Verhalten | rot | Subscriptions persistieren NICHT über Reconnect — Re-Subscribe-Pflicht |

`CPWebSocketClient` (`cp/ws_client.py`, ab v1.10.0) ist als
wiederverwendbarer Baustein implementiert: connect, Auth-Frame,
`sts.authenticated=true`-Wait, async Frame-Iteration, send,
`tic`-Ping-Loop, exponential-backoff Reconnect. **Single-Owner-Konstraint:
pro Instanz nur ein `connect()`.**

### 7.3 Phase 2 (AP-04 K5/K6, geplant)

K5 ist ein Consumer-Fragebogen (PSM, trading-robot) zu Topics, SLOs,
Symbol-Skala, Failure-Modes. K6 entscheidet das WS-Adapter-Design
(Subscription-Manager, Topic-Adapter, EventBus-Producer). Die Anbindung
an `/v1/quotes/stream` und `/v1/events/stream` (Migration von
REST-Polling auf WS-Push) wird in einem Folge-AP-05 spezifiziert,
nicht in AP-04.

### 7.4 WS-Lifespan-Aktivierung (AP-11 K9)

> **Backend-Hinweis:** Der WS-Push-Pfad ist cp-spezifisch — er nutzt
> die WebSocket-Quelle des CP Gateways (`/v1/api/ws`). Im
> `BG_BACKEND=tws`-Pfad gibt es heute keinen aktiven Stream-Pfad; die
> TWS-Event-Callbacks (`updateEvent`, `execDetailsEvent`,
> `orderStatusEvent`) werden in der Single-Owner-Coordination-Karte 4
> in den `EventBus` gebridged. Bis dahin ist `/v1/quotes/stream` /
> `/v1/orders/stream` unter `BG_BACKEND=tws` nicht funktional.

Der WS-Push-Pfad ist Code-seitig komplett (AP-11 K1..K8: SmdTopicAdapter,
SubscriptionRegistry, WSPushSource, OrdersBroadcaster, /v1/status).
K9 hat das Lifespan-Wiring nachgezogen: `BG_QUOTES_SOURCE=ws` schaltet
den `/v1/quotes/stream`-Pfad bei Service-Start vom REST-Polling auf
WS-Push. Der Default bleibt `polling` — wer den WS-Pfad nutzen will,
trägt den ENV-Wert in `.env` ein.

Bedingungen für die Aktivierung (alle drei müssen gelten, sonst
Fallback auf Polling mit Warning im Log):

1. `BG_QUOTES_SOURCE=ws` ist gesetzt.
2. `AuthLifecycle.snapshot().auth_status == OK` beim Start (sprich:
   die initiale Tickle-Antwort ist `authenticated=true`).
3. `AuthLifecycle.snapshot().session_id` ist gefüllt — die Session-ID
   wird aus dem `session`-Key der Tickle-Antwort übernommen.

**Restart-Disziplin:** Der Browser-2FA-Login auf den CP-Gateway-
Container muss VOR dem `gateway`-Service laufen. Wenn der Lifespan
beim Start `auth_status != OK` oder eine leere `session_id` sieht,
fällt er auf Polling zurück und loggt eine Warnung. Ein späterer
Login (während der Service schon läuft) hebt den Polling-Pfad nicht
mehr auf — dafür braucht es einen Service-Restart.

Cookies für den WS-Handshake werden aus dem httpx-CookieJar des
REST-Clients gelesen und als `Cookie:`-Header beim Upgrade
mitgeschickt. Die Session bleibt damit konsistent zu den REST-Calls
desselben Containers.

Live-Smoke nach Aktivierung:

```bash
# Auf cma-pi-1, nach Login:
docker compose --env-file .env up -d gateway   # mit BG_QUOTES_SOURCE=ws in .env
curl -s -H "Authorization: Bearer $TOKEN" \
    http://localhost:4000/v1/status | jq .
# erwartet: cp_gateway_connected=true, reconnect_attempt=0
```

---

## 8. Logging-Architektur

Stand: AP-05 K1 (Logging-Backbone, v1.9.0) und K2 (Inbound-Body-Logging,
v1.10.0) sind live. K3 (CP-Wire-Hook) und K4ff folgen.

### 8.1 Drei Log-Stränge

Alle Stränge schreiben **JSON-Lines** durch denselben structlog-
JSONRenderer. Routing per Logger-Name auf separate Sinks:

| Logger-Name | Datei | Inhalt | Status |
|---|---|---|---|
| `broker_gateway.http` | `inbound.log` | Consumer -> broker-gateway HTTP (Metadaten + Bodies) | live |
| `broker_gateway.cp.wire` | `cp_wire.log` | broker-gateway -> IBKR CP HTTP-Roundtrip | Hook kommt mit AP-05 K3 |
| `broker_gateway` | `app.log` | Lifecycle, Throttle, Subscriptions, Streams, Recorder, alles übrige | live |

`propagate=False` auf den drei Strang-Loggern verhindert Cross-Talk.
Ohne `BG_LOG_DIR` schreiben alle drei auf stdout (Backwards-Kompatibilität).

### 8.2 Korrelation per `request_id`

Die Observability-Middleware (`middleware/observability.py`) erzeugt
pro Inbound-Request eine `request_id` und bindet sie über
`structlog.contextvars.bind_contextvars`. Damit erscheint sie automatisch
in jedem nachgelagerten Event derselben Verarbeitung — auch in
`cp_wire`-Events, sobald der Wire-Hook scharfgeschaltet ist. Inbound-
und CP-Roundtrips lassen sich darüber rekonstruieren.

### 8.3 Header-Redaktion (SSOT)

`src/broker_gateway/cp/redaction.py` ist die einzige Stelle, an der
festgelegt wird, welche Header-Werte vor dem Logging zu entfernen
sind: `Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`,
`X-Auth-Token`, `Proxy-Authorization`. Verwendet vom CP-Recorder, vom
Inbound-Body-Middleware und (kommend) vom CP-Wire-Hook. Token-Werte
landen niemals auf Disk.

### 8.4 Body-Logging

Inbound-Bodies werden 1:1 geschrieben — keine Redaction, keine
Truncation. SSE-Antworten (`text/event-stream`) materialisieren wir
nicht (`response_streaming: true`, `response_body: null`).
Notfall-Schalter `BG_LOG_INBOUND_BODIES=off` deaktiviert Body- und
Header-Felder im `http_request`-Event; Metadaten bleiben unverändert.

### 8.5 Rotation

`logging.handlers.RotatingFileHandler` pro Strang. Default
10 MiB, 20 Backups. Pro-Strang-Override via
`BG_LOG_INBOUND_MAX_BYTES`, `BG_LOG_CP_WIRE_MAX_BYTES`,
`BG_LOG_APP_MAX_BYTES` und entsprechenden `..._BACKUP_COUNT`.

### 8.6 Prometheus-Metrics (`/metrics`)

Custom-Collectoren lesen Gauges beim Scrape live aus den Singletons
(AuthLifecycle, SubscriptionManager, ThrottleManager) — keine
Stale-State-Probleme.

| Metrik | Typ | Labels |
|---|---|---|
| `broker_gateway_requests_total` | Counter | `path`, `status`, `scope` |
| `broker_gateway_request_latency_seconds` | Histogram | `path` |
| `broker_gateway_pacing_violations_total` | Counter | `class` |
| `broker_gateway_session_age_seconds` | Gauge | — |
| `broker_gateway_subscription_count` | Gauge | — |
| `broker_gateway_throttle_extra_wait_seconds` | Gauge | `class` |

---

## 9. Test-Strategie

Vier Schichten — keine läuft gegen Live-IBKR im Default-Workflow.

### 9.1 In-Process Mock (heute, default)

`pytest` startet die FastAPI-App in-process gegen einen
`cp_gateway_mock`-Fixture (`tests/conftest.py`). Statische Bodies werden
ab v1.2.0 aus `tests/fixtures/recorded/` geladen — **`live/` hat
Vorrang vor `seed/`**. Stateful Endpunkte (snapshot, Orders-Lifecycle,
Trades-Schleife, unsubscribe) generieren Antworten weiterhin im Code
in `tests/cp_mock/replay.py`.

Konfigurierbare Fixture-Flags ohne eigene Mocks:

| Flag | Wirkung |
|---|---|
| `auth_lost` | `/iserver/auth/status` liefert `authenticated=false` |
| `slow_response_ms` | künstliche Latenz pro Request in Millisekunden |
| `pacing_violation_after_n` | nach N Requests HTTP 429 für jeden weiteren |

Stand: 381 pytest-Tests passieren ohne Live-Verbindung.

### 9.2 Record-and-Replay (AP-02, AP-03, AP-04)

`CPGatewayClient` enthält einen httpx-Event-Hook (`cp/recorder.py`),
der Live-HTTP-Verkehr als deterministische JSON-Fixtures unter
`tests/fixtures/recorded/` ablegt — aktiviert ausschließlich über
`BG_CP_RECORD_DIR`. Vor dem Schreiben:
- Authorization-/Cookie-/API-Key-Header werden gefiltert
  (`cp/redaction.py`).
- Timestamps und Order-/Execution-/Session-IDs werden durch
  Platzhalter ersetzt (`cp/normalize.py`).

Konzept und Diff-Bewertung in `docs/cp-recordings.md`. Live-Recordings
gegen U25235077 (heute aktiver Live-Account; Cutover auf dediziertes
Service-Konto geplant — siehe Sektion 11.2 und
[`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md))
leben unter `tests/fixtures/recorded/live/`, WebSocket-Mitschnitte
(AP-04 K2/K4) unter `tests/fixtures/recorded/ws/`. Cassettes bleiben
nach dem Cutover unverändert — sie sind deterministische Mocks, kein
Hinweis auf das aktive Live-Konto.

### 9.3 Drift-Detection (AP-03, ab v1.5.0 in Betrieb)

Zwei komplementäre Mechanismen:

- **Doku-Drift (Frühwarner):** systemd-Timer auf cma-pi-1 zieht
  täglich die IBKR-OpenAPI-Spec, vergleicht gegen
  `docs/research/ibkr-cpapi-doc.json`. Bei Diff legt das Skript eine
  KanPrompt-Karte an (blocked=true bei breaking).
- **Live-Drift (Build-Acceptance):** `scripts/check_mock_drift.py
  --build-acceptance` läuft beim Container-Rebuild als Acceptance-Gate.
  Build schlägt fehl, wenn Mock-Fixture und Live-Antwort divergieren.

### 9.4 Paper-Test-Pyramide (AP-06..AP-11, geplant)

Zweite, parallel laufende `broker-gateway`-Instanz auf IBKR-Paper-Konto,
deployed aus demselben git-Stand wie Live. Einziger Unterschied: `.env`
(Paper-Login, Paper-Account-ID, eigener Compose-Project-Name, eigene
Volumes/Ports — geplant cpgateway-paper:5001, gateway-paper:4001).
Test-Harness mit vier Aggressivitäts-Markern (`paper_readonly`,
`paper_safe_write`, `paper_pic`, `paper_destructive`); pytest.ini
deaktiviert sie alle im Default-Lauf. Sicherheits-Schranken
unabhängig vom Marker: Paper-Account-Whitelist (`account_id`
beginnt mit `DU`), `max_notional_per_order`, `max_open_orders`,
globaler Kill-Switch `BG_PAPER_TESTS_DISABLED`.

Erst Lese-Stufe (AP-08, `paper_readonly`) ist als Karten geplant; sie
prüft Adapter-Verhalten gegen die deployed Paper-Instanz und füllt
gleichzeitig eine wachsende Cassette-Schicht in
`tests/fixtures/recorded/paper/{date}/`.

---

## 10. Was bewusst NICHT in v1 ist

- **Multi-Account-Support.** v1 spricht mit genau einem Konto.
  Multi-Account braucht eine zweite Service-Instanz pro Konto — ist
  Konsequenz aus dem Singular-Halter-Prinzip (3.1).
- **Multi-Broker-Support.** v1 ist auf IBKR-CP-Gateway angeschnitten.
  Adapter-Pattern für andere Broker ist denkbar in v2/v3, aber jetzt YAGNI.
- **Historische Marktdaten** (Bars, EOD-Series). PSM nutzt yfinance und
  andere Sources. `broker-gateway` ist Realtime-fokussiert.
- **Options-Chains, FOPs, Futures** in v1. Erst wenn der Account
  entsprechende Permissions hat. Bis dahin: Stocks + Cash (FX).
- **Order-Routing-Strategien** (Smart-Routing-Konfigurationen,
  OCA-Gruppen). v1 nimmt einfache Order-Typen (`LMT`, `MKT`, `STP`,
  `STP-LMT`) und routet via IBKR-Default.
- **Komplexe Auth-Pipelines** (OAuth, OIDC). v1 nutzt opake API-Tokens
  mit Scope-Claims, intern in Memory oder File.
- **Frontend / UI.** Nur API.
- **Persistente Geschäftsdaten.** Service ist transient — In-Memory-Caches,
  optional Redis für Restart-Persistenz.

---

## 11. Verweise und offene Fragen

### 11.1 Verweise

- API-Contract: [`docs/api/v1.md`](api/v1.md)
- Recording-Konzept: [`docs/cp-recordings.md`](cp-recordings.md)
- WS-Findings: [`docs/research/ibkr-cpapi-websockets-findings.md`](research/ibkr-cpapi-websockets-findings.md)
- WS-Use-Cases: [`docs/research/ibkr-cpapi-use-cases.md`](research/ibkr-cpapi-use-cases.md)
- Login-Runbook: [`docs/runbooks/cpgateway-login.md`](runbooks/cpgateway-login.md)
- Troubleshooting: [`docs/runbooks/cpgateway-troubleshooting.md`](runbooks/cpgateway-troubleshooting.md)
- Doc-Drift: [`docs/runbooks/doc-drift-check.md`](runbooks/doc-drift-check.md)
- Mock-Drift: [`docs/runbooks/mock-drift-check.md`](runbooks/mock-drift-check.md)
- Account-Cutover: [`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md)
- Bootstrap-Historie: [`docs/01-context-from-bootstrap-session.md`](01-context-from-bootstrap-session.md)

### 11.2 Offene Architektur-Fragen

Hier festgehalten, weil sie quer zu mehreren APs liegen — werden im
Verlauf als Karten umgesetzt, **nicht** in diesem Dokument
eigenmächtig entschieden:

- **Stream-Transport für Consumer:** SSE bleibt für `quotes:read` und
  `events:read`; ob Consumer zusätzlich einen WebSocket angeboten
  bekommen, entscheidet AP-04 K6 nach Auswertung des Consumer-Fragebogens.
- **WS-Adapter-Architektur:** Subscription-Manager-Refactor +
  Topic-Adapter + EventBus-Producer aus Findings ableiten — Zielbild
  für Folge-AP-05 (separat von Logging-AP-05; Naming-Kollision
  beobachten).
- **Reauthenticate-Strategie nach Pause:** **geklaert** (AP-09,
  Mai 2026). Volles Runbook in
  [`docs/runbooks/cpgateway-session-resume.md`](runbooks/cpgateway-session-resume.md):
  vor 2FA erst `POST /iserver/reauthenticate`, dann Drift-Check 2x mit
  90 s Warmup-Pause; Eskalation auf Browser-2FA erst, wenn beide
  Drift-Checks scheitern.
- **TLS / Reverse-Proxy:** aktuell HTTP-only intern (Tailscale),
  externer TLS-Endpunkt steht noch nicht zur Diskussion.
- **Idempotency-Storage Restart-Persistenz:** Redis als optionales
  Backing für `idempotency.py` ist im Design erwähnt, aber noch nicht
  implementiert. Solange der Service in-process restartet, ist die
  Memory-Map akzeptabel.
- **TWS-Backend-Migration:** seit v1.34.0 läuft `tws/` parallel zu
  `cp/`, gewählt per `BG_BACKEND`. Nächste Schritte sind in der
  KanPrompt-Backlog: **Karte 4 (Single-Owner-Coordination)** macht die
  Service-Schicht (Portfolio/Orders/Quotes/Events) TWS-fähig und
  bridged TWS-Event-Callbacks in den EventBus; **Karte 6 (Hard-Cutover)**
  reisst den cp-Pfad raus, entfernt `TWSLifecycleCpAdapter` und
  ersetzt den `cpgateway`-Compose-Service durch einen `tws`-Service
  mit Healthcheck. Bis dahin sind die Order/Portfolio/Quotes-Pfade
  unter `BG_BACKEND=tws` nicht funktional.
- **Account-Identitaet-Wechsel — Status:** seit 2026-05-18 ist ein
  zweites IBKR-Konto als dediziertes Service-Konto beantragt; die
  konkrete Account-ID und die Login-Credentials sind noch nicht
  verfügbar. Ziel: broker-gateway-Live hängt zukünftig am Service-
  Konto, das Privatkonto U25235077 bleibt frei für Operator-Browser-
  Logins ohne Single-Session-Hijack der Service-Instanz (3.1 / 2.1).
  Pfad in drei Karten: Phase 1 (Doku-Vorbereitung, diese Sektion +
  3.1 + 6.4/10.3/12.2 in `04-security.md` + Glossar-Eintrag +
  [`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md)),
  Phase 2 (eigentlicher Cutover, blocked bis Account-Daten vorliegen),
  Phase 3 (U25235077-Bereinigung in Doku + Memory, blocked durch
  Phase 2). U25235077 bleibt bis zur Phase 3 in Doku-Texten als
  "heute aktiv" sichtbar; Cassettes unter
  `tests/fixtures/recorded/live/` werden **nicht** umgeschrieben.

---

*Stand: v1.34.1 (2026-05-09). Lebt mit dem Code: jede Karte mit
architekturrelevanten Konsequenzen aktualisiert dieses Dokument oder
verweist explizit auf die Sektion, die geändert werden muss.*
