# WS-Adapter-Architektur-Design (AP-04 K6, Decision-Gate für Folge-AP)

**Stand:** 2026-05-01 (rev. 2 nach User-Review) · **Verantwortlich:**
Christian Mangold · **Status:** Decision-Gate freigegeben — Anlage des
Folge-AP (Vorschlag-Titel **„AP-11 WS-Adapter Implementation"**) in
einem getrennten `outline`-Lauf.

> **Was diese Datei ist.** Konsolidierter Architektur-Schnitt für den
> produktiven WebSocket-Adapter zwischen `CPWebSocketClient` (K3) und
> den Consumern (PSM, trading_robot). Sie verdichtet Phase-1-Findings
> aus K1–K4, die Consumer-Antworten aus den beiden Sibling-Projekten
> und die Bestandsarchitektur des Service. Das Implementations-AP
> selbst ist nicht Teil dieser Karte.
>
> **Was diese Datei nicht ist.** Kein Production-Code, keine
> verbindliche Karten-Anlage. Karten-Skizzen in Sektion 7 sind
> Vorschläge, die im `outline`-Lauf für AP-11 verbindlich werden.
>
> **Revisions-Hinweis.** Rev. 2 setzt vier User-Direktiven aus dem
> ersten Review um (Single-Tenant zementiert, WS-Egress als zweites
> Ziel-Tier, Failure-Mode-Header und N-1-Schema-Compat zurückgenommen,
> Single-User-Fokus mit Funktion vor Sicherheit) und ergänzt drei
> Schichten Handelszeit-Information mit börsenzentriertem Kalender.

---

## 0. Quellen

| Quelle | Pfad / Karte | Inhalt |
|--------|--------------|--------|
| Phase-1-Findings (K1) | [`docs/research/ibkr-cpapi-websockets-findings.md`](../research/ibkr-cpapi-websockets-findings.md) | Setup-Realität, Frame-Format, Heartbeat, `tic`-Quirk |
| Recording-Schema (K2) | `tests/cp_mock/ws_replay.py`, `tests/fixtures/recorded/ws/spike-baseline.jsonl` | JSONL-Format für Replay |
| WS-Client (K3) | `src/broker_gateway/cp/ws_client.py` | Connect, Auth, `tic`-Loop, Reconnect mit Backoff |
| Topic-Exploration (K4) | dieselbe Findings-Datei, Sektion *Topic-Exploration* | smd/sor/str-Verhalten, Reife-Ranking |
| K5 Fragenkatalog | [`docs/architecture/ws-consumer-questions.md`](ws-consumer-questions.md) | Reiner Katalog, Antworten in Consumer-Repos |
| PSM-Antwort | `personal_stock_manager/docs/integrations/broker-gateway.md` (PSM-Repo, KanPrompt-Karte `a1037c45-b4af-42bc-abd4-8a2ac015ceeb`) | UI-getrieben, REST-Polling als eigene Reaktion auf SSE-Stillstand, 150-Symbol-Cap |
| trading_robot-Antwort | `trading_robot/docs/integrations/broker-gateway.md` (trading_robot-Repo, KanPrompt-Karte `e71623d2-bd8d-4643-a15b-6d93c1afafd5`) | Trading-kritisch, Fail-Loud, 30-Symbol-Cap, p95<150 ms |

Bestandscode-Bezug: `streams/manager.py` (SubscriptionManager mit
Refcount und Fan-Out, heute Polling-basiert), `cp/lifecycle.py`
(AuthLifecycle, Heartbeat-/Reauth-Loop), `api/v1/quotes_stream.py`
(SSE-Endpoint `/v1/quotes/stream`), `api/v1/orders_stream.py`
(SSE-Endpoint `/v1/orders/stream`, Order-Lifecycle-Frames — der
frühere `events_stream.py`/`/v1/events/stream` wurde mit Karte
`37fca2f3` ersatzlos entfernt), `api/v1/instruments.py` (REST-Endpoint
`/v1/instruments/...`).

---

## 1. Zielbild

### 1.1 Annahmen aus Rev.-2-Review

- **Single-Tenant.** Genau ein User pro broker-gateway-Instanz. Kein
  Multi-Tenant-Hardening, keine Per-Konto-Token-Scopes,
  keine Routing-Komplexität für mehrere PSM-Endkunden. Erweiterung ist
  ein eigenständiges Re-Design, falls jemals nötig.
- **Funktion vor Sicherheit (in dieser Phase).** Failure-Mode-Header,
  Schema-Versionierungs-Vertrag mit N-1-Compat und feingranulare
  Scopes sind explizit zurückgestellt. Der Adapter darf einfach sein.
- **WebSocket als Ziel-Transport zum Consumer.** SSE bleibt Pflicht-
  Pfad (Phase A), WS-Egress kommt parallel als zweites Ziel-Tier
  (Phase B) — beide Endpunkt-Familien existieren nebeneinander, der
  Consumer wählt pro Verbindung.
- **Schema synchron zwischen Adapter und Consumern.** Die Consumer
  stehen unter eigener Kontrolle; Schema-Änderungen werden in kleinen
  Schritten gemacht und beidseitig synchron eingeführt. Kein
  Pflicht-Versionsfeld pro Frame, kein Compat-Korridor.

### 1.2 Topic-Entscheidungen

| Topic | Status in AP-11 | Begründung |
|-------|-----------------|------------|
| `smd` (Market-Data Snapshot/Delta) | **WS-integriert (Phase A)** | Beide Consumer brauchen es; K4-Reife grün; Refcount-Layer existiert bereits. Erste Iteration zielt nur auf `smd`. |
| `sor` (Order-Lifecycle) | **WS-integriert (Phase B)** | Beide Consumer brauchen es; trading_robot blockiert Live-Trade ohne `sor`-Push; REST-Bootstrap (`/iserver/account/orders`) ist Pflicht-Vorlauf, weil IBKR keinen Initial-Snapshot über WS garantiert. |
| `str` (Trades / Executions) | **REST-Polling, kein Push** | trading_robot lehnt Push für v1 ab (Re-Send-Quirk, `execution_id`-Dedup-Pflicht), PSM nimmt es nur optional. Bestehender Trades-Endpunkt bleibt. |
| `spl` (Profit/Loss) | **Nicht integriert** | Beide Consumer rechnen P/L selbst; zweite Wahrheit ist SSOT-Verstoß. |
| `smh` (Market-Data History) | **Nicht integriert** | Historische Daten kommen aus REST/PSM bzw. externen Quellen. |
| `sbd` (Bulletin) | **Nicht integriert** | Kein Bot-Logik-Input, kein UI-Hook. |

### 1.3 Neu in Rev. 2: Handelszeit- und Tradeability-Information

Drei Schichten, die der Adapter bündelt — Details in Sektion 5 und in
**Anhang C**:

1. **Statisch pro Börse (Kalender).** REST `/trsrv/secdef/schedule`
   liefert einen 14-Tage-Schedule pro Börse mit Sessions inkl.
   Pre-/Post-Market und impliziten Feiertagen. Adapter cached pro
   `exchange_id`, TTL 12h.
2. **Symbol→Börse-Verknüpfung.** `/iserver/contract/{conid}/info`
   liefert `listingExchange`/`primaryExchange` pro `conid`. Adapter
   cached pro `conid`, TTL 24h.
3. **Live-Tradeability pro Symbol.** Aus `smd`-Frame-Feldern (`6509`
   Availability-Code, `7295` Trading-Hours-Hint, `7296` Liquid-Hours-
   Hint) leitet der Adapter abgeleitete Felder `is_tradeable_now: bool`
   und `current_session: rth|pre|post|closed|halted` ab und reicht sie
   im normalisierten `smd`-Stream-Frame mit aus.

### 1.4 Reihenfolge-Bedingungen

- `smd`-WS-Push muss vor dem ersten Paper-Trading-Test im
  trading_robot-Repo stehen (sonst kein verlässlicher Live-Quote-Pfad
  für den Paper-Modus).
- `sor`-WS-Push muss vor dem ersten echten Live-Trade stehen.
- AP-11 läuft in mehreren Phasen — `smd` zuerst (Phase A inkl.
  SSE-Pfad), `sor` und der WS-Egress später (Phase B). PSM hat
  **kein** hartes Datum (Q3-2026 frühestens, nach PSM-M2-Karte `225`).

### 1.5 Was bewusst draußen bleibt

- Per-Subscription-Failure-Mode-Header. Default ist `fail-loud`
  (Stream-Close mit Error-Frame). Wer REST-Fallback braucht, baut den
  als Consumer-Logik (PSM macht das ohnehin schon im REST-Polling-
  Daemon). Adapter bleibt einfach.
- Schema-Versionierungs-Pflichtfeld pro Frame. `schema_version` darf
  als optionales Frame-Feld vorhanden sein, ist aber kein
  Vertragselement; Schema-Änderungen werden bilateral koordiniert.
- Multi-Tenant-Adapter (mehrere PSM-Endkunden auf einer
  broker-gateway-Instanz). Single-Tenant zementiert.
- Per-Konto-Token-Scopes (z.B. `read:streams.quotes:U25235077`).
  Bleibt Coarse-Scope. Einführung erst, wenn ein zweites Konto
  realistisch wird.
- Gemultiplexer Single-Stream (alle Topics auf einer SSE/WS-
  Connection). Beide Consumer kommen mit getrennten Streams aus.

---

## 2. Komponenten-Diagramm

```
+---------------------------+        +---------------------------+
| trading_robot             |        | personal_stock_manager    |
|   MarketDataPort-Adapter  |        |   Quotes-Daemon           |
|   BrokerPort-Adapter      |        |   Order-View              |
+-------------+-------------+        +-------------+-------------+
              | SSE (Pflicht-Pfad, Phase A)                     |
              | WS  (Ziel-Pfad, Phase B, parallel verfuegbar)    |
              v                                                 v
+-----------------------------------------------------------------+
| broker-gateway · /v1                                            |
|                                                                 |
|  +------------------+ +------------------+ +-------------------+|
|  | quotes_stream    | | quotes_ws        | | orders_stream     ||
|  | /v1/quotes/stream| | /v1/quotes/ws    | | /v1/orders/stream ||
|  | (SSE, Bestand)   | | (WS, neu B)      | | (SSE, neu A/B)    ||
|  +---------+--------+ +---------+--------+ +---------+---------+|
|  +------------------+                                  | +-----+|
|  | orders_ws        |                                  | | ... ||
|  | /v1/orders/ws    |                                  | +-----+|
|  | (WS, neu B)      |                                  |        |
|  +---------+--------+                                  |        |
|            |                                           |        |
|            v   beide Egress-Tiers schoepfen aus        v        |
|  +-----------------------------------------------------------+  |
|  | StreamHub (Refcount + Fan-Out)                            |  |
|  |   - existierende SubscriptionManager-Logik fuer smd       |  |
|  |   - neuer OrdersBroadcaster fuer sor                      |  |
|  |   - Quelle: WS-Push als 1st-class, REST-Polling als opt.  |  |
|  +---------+----------------------------+----------------+----+ |
|            ^                            ^                ^      |
|  +---------+-----------+   +------------+----------+     |      |
|  | TopicAdapter "smd"  |   | TopicAdapter "sor"    |     |      |
|  |   - Frame-Parser    |   |   - Lifecycle-Mapper  |     |      |
|  |   - Field-Decoder   |   |   - Bootstrap-Glue    |     |      |
|  |   - Snapshot-Merge  |   |     (REST-Initial)    |     |      |
|  |   - Tradeability-   |   |                       |     |      |
|  |     Feld-Ableitung  |   |                       |     |      |
|  +---------+-----------+   +------------+----------+     |      |
|            |                            |                |      |
|            +-------------+--------------+                |      |
|                          v                               |      |
|              +---------------------+                     |      |
|              | SubscriptionRegistry|                     |      |
|              |   - Soll-State je   |---- Replay nach     |      |
|              |     Topic+Args      |     Reconnect       |      |
|              |   - re-applies on   |                     |      |
|              |     CPWebSocketClient.connect()           |      |
|              +----------+----------+                     |      |
|                         |                                |      |
|                         v                                |      |
|              +---------------------+                     |      |
|              | CPWebSocketClient   |  (K3, Bestand)      |      |
|              +----------+----------+                     |      |
|                         |                                |      |
|                         v                                |      |
|              +---------------------+    +----------------+----+ |
|              | AuthLifecycle       |    | StatusEndpoint      | |
|              | (cp/lifecycle.py)   |    | /v1/status          | |
|              +----------+----------+    +---------------------+ |
|                         |                                       |
|              +----------+----------+                            |
|              | CalendarService     |    +---------------------+ |
|              |   /trsrv/secdef/    |    | InstrumentsAPI      | |
|              |   schedule          |--->| /v1/exchanges/...   | |
|              |   12h-Cache pro     |    | /v1/instruments/... | |
|              |   exchange_id       |    +---------------------+ |
|              +----------+----------+                            |
|                         |                                       |
|                         v                                       |
|                +-------+-------+                                |
|                |  CP-Gateway   |                                |
|                |  (intern,     |                                |
|                |   Compose)    |                                |
|                +---------------+                                |
+-----------------------------------------------------------------+
```

### 2.1 Neue Layer (gegenüber heute)

| Layer | Zweck | Wo | Bestand? |
|-------|-------|----|----------|
| `SubscriptionRegistry` | Hält den **Soll-State** aller aktiven Subscriptions (Topic + Args + Consumer-Refs). Replayt nach `CPWebSocketClient.connect()` alle Subscribes erneut, weil der Server den State nicht persistiert (K4-Reconnect-Befund). | `streams/registry.py` (neu) | nein |
| `TopicAdapter` (pro Topic) | Parsed Frame-Schema (mixed types: Strings für Preise, Floats für %-Change), normalisiert Felder auf semantische Namen, deduppt `tic`/`smd`-Doppel-Frames, baut Voll-Snapshot pro Frame aus Delta-Updates. `smd`-Adapter leitet zusätzlich `is_tradeable_now`/`current_session` aus Live-Feldern + `CalendarService` ab. | `cp/topics/smd.py`, `cp/topics/sor.py` (neu) | nein |
| `WSPushSource` für `StreamHub` | Neue Quelle neben dem Polling-Pfad; abonniert beim Topic-Adapter, schiebt Frames in den vorhandenen Refcount-/Fan-Out-Mechanismus. | `streams/ws_source.py` (neu) | nein |
| WS-Egress-Endpunkte (Phase B) | `/v1/quotes/ws`, `/v1/orders/ws` — direkter WebSocket-Zugang, identisches Frame-Format wie SSE-Pfad. Nutzen denselben `StreamHub`. | `api/v1/quotes_ws.py`, `api/v1/orders_ws.py` (neu) | nein |
| `OrdersBroadcaster` + `/v1/orders/stream` | SSE-Endpoint, analog zu `quotes_stream.py`. Initialer REST-Bootstrap via existierenden CP-REST-Client, dann Live-Frames aus `sor`-TopicAdapter. | `streams/orders.py`, `api/v1/orders_stream.py` (neu) | nein |
| `CalendarService` | Holt börsenzentrierten Schedule via `/trsrv/secdef/schedule`, cached pro `exchange_id` (TTL 12h), liefert sowohl an den Adapter (für Tradeability-Felder im `smd`-Frame) als auch an den Calendar-Endpoint. | `cp/calendar.py` (neu) | nein |
| `StatusEndpoint` (`/v1/status`) | Beobachter-API. Felder: `cp_gateway_connected`, `last_frame_age_seconds`, `reconnect_attempt`, `subscriptions_active`. PSM polled 10 s, trading_robot konsumiert vor jeder Order-Entscheidung. | `api/v1/status.py` (neu) | nein |
| Calendar-/Instruments-Erweiterung | `/v1/exchanges/{exchange_id}/calendar`, `/v1/exchanges`, plus `exchange_id` im bestehenden `/v1/instruments/{conid}`-Response. | `api/v1/exchanges.py`, Edit `api/v1/instruments.py` | teilweise (instruments.py existiert) |

### 2.2 Beziehung zu `AuthLifecycle`

Unverändert gegenüber Rev. 1: `CPWebSocketClient` läuft parallel zum
bestehenden `AuthLifecycle`-Tick. Auth-Verlust an einer der beiden
Stellen pausiert die Subscriptions, Resubscribe nach Reauth.
`AuthLifecycle.reauthenticate(force=True)` bleibt das einzige
Reauth-Tor; der WS-Adapter ist Konsument, nicht Co-Owner.

---

## 3. Consumer-API

### 3.1 SSE als Pflicht-Pfad (Phase A)

Bestehender `/v1/quotes/stream`-Endpoint bleibt **vertragsgleich**,
nur die Quelle wechselt von REST-Polling auf WS-Push:

| Garantie | Heute | Nach Phase A |
|----------|-------|--------------|
| Pfad | `/v1/quotes/stream?conids=…` | unverändert |
| Frame-Format | semantische Felder | unverändert (Delta-Snapshot) |
| `Last-Event-ID`-Reconnect | ja | ja |
| Liveness | SSE-Comment-Heartbeat alle 15 s | unverändert |
| Latenz `smd` p95 | ~1 s (Polling-Takt) | < 150 ms (Robot-SLO) |
| Tradeability-Felder | nicht vorhanden | neu im Frame: `is_tradeable_now`, `current_session` |

Neu: SSE-Endpoint `/v1/orders/stream` mit identischem Pattern, REST-
Bootstrap als ersten Frame, dann Live-Push.

### 3.2 WS-Egress als Ziel-Pfad (Phase B)

Parallel und additiv zu SSE:

- `GET /v1/quotes/ws?conids=…` — WebSocket-Upgrade, identisches
  Frame-Format wie der SSE-Pfad. Frames werden als Text-Frames mit
  JSON-Body gesendet.
- `GET /v1/orders/ws?account=…` — analog für `sor`.
- Authentifizierung: Bearer-Token im `Sec-WebSocket-Protocol`-Header
  (`Sec-WebSocket-Protocol: bearer-<jwt>`) oder als Query-Param
  `?token=…` (für Browser-Clients ohne Header-Kontrolle). Scope-
  Anforderung identisch zum SSE-Pfad.
- `Last-Event-ID`-Äquivalent: optionaler `last_event_id`-Query-Param
  beim WS-Connect; Server replayt aus dem Snapshot-Cache.
- Heartbeat: WS-Ping/Pong alle 15 s (Browser-Standard); kein
  Application-Level-Heartbeat zusätzlich.

WS- und SSE-Pfad bedienen sich aus demselben `StreamHub`. Ein und
derselbe Refcount-Slot (gleicher `conid`/Account) wird von allen
Pfaden gleichzeitig genutzt.

### 3.3 Frame-Schema

- Frame-Body ist semantisches JSON, identisch zwischen SSE und WS.
- **Optionales** `schema_version: int`-Feld pro Frame als
  Diagnose-Hilfe — kein Pflichtteil des Vertrags. Schema-Änderungen
  werden zwischen Adapter und Consumer-Repos synchron koordiniert.
- Felder pro Topic in **Anhang A** (`sor`) und **Anhang B** (`smd`).
- **Tradeability-Felder im `smd`-Frame** (neu in Rev. 2):
  - `is_tradeable_now: bool` — true wenn `current_session ∈
    {rth, pre, post}` und `availability_code` keinen Halt anzeigt.
  - `current_session: "rth"|"pre"|"post"|"closed"|"halted"` —
    abgeleitet aus `availability_code` (Live) und gecachtem Schedule
    der Symbol-Heimat-Börse.

### 3.4 Failure-Mode

Default `fail-loud` für **alle** Streams (SSE und WS):

- WS/SSE-Stream-Close mit Error-Frame, sobald die zugrundeliegende
  CP-Gateway-Verbindung tot ist und der Reconnect den Backoff-Cap
  (30 s) erreicht.
- Consumer ist für Reaktion zuständig. PSM hat einen REST-Polling-
  Daemon, den es als eigene Reaktion auf SSE-Stillstand starten kann
  — das ist Consumer-Logik, nicht Adapter-Komplexität.
- Kein Per-Subscription-Header, kein REST-Auto-Fallback im Adapter.
  Vereinfachung gegenüber Rev. 1 nach User-Direktive.

### 3.5 Authentifizierung & Scopes

Coarse-Scopes wie in `docs/04-security.md`:

| Scope | Topic / Pfad |
|-------|--------------|
| `read:streams.quotes` | `/v1/quotes/stream`, `/v1/quotes/ws` |
| `read:streams.orders` | `/v1/orders/stream`, `/v1/orders/ws` |
| `read:orders` | REST-Bootstrap, REST-Reconcile |
| `write:orders` | bestehender Order-Submit-Pfad |
| `read:status` | `/v1/status` |
| `read:instruments` | `/v1/instruments/...`, `/v1/exchanges/...` |

Per-Konto-Granularität bleibt bewusst weg — Single-Tenant.

---

## 4. Failure-Mode-Strategie

### 4.1 WS-Abbruch (CP-Gateway erreichbar, WS tot)

Unverändert gegenüber Rev. 1, ohne Per-Sub-Header-Logik:

1. `SubscriptionRegistry.replay()` nach jedem erfolgreichen
   `connect()` — alle aktiven Subscribes neu absetzen.
2. Status-Frame `subscription_replay_in_progress` an alle abhängigen
   Consumer-Streams, bis Replay durch ist.
3. Erster Frame nach Reconnect kann merklich später kommen;
   Status-Endpoint zeigt Reconnect-Phase.
4. Reicht der Reconnect-Backoff-Cap (30 s) nicht aus, wird der
   Consumer-Stream mit Error-Frame geschlossen (`fail-loud`).

### 4.2 CP-Gateway-Restart

Wie WS-Abbruch plus REST-Reauth:

1. WS-Reader sieht Disconnect.
2. `AuthLifecycle.reauthenticate(force=True)` (entspricht der
   Pause-Wiederaufnahme-Sequenz aus der Auto-Memory).
3. Sobald `AuthStatus == AUTHENTICATED`, `CPWebSocketClient.connect()`
   neu, `SubscriptionRegistry.replay()`.
4. Optional `gateway_restarting`-Status-Frame im Stream.

### 4.3 Reconnect-Backoff (Adapter ↔ CP-Gateway)

Bestand: `_DEFAULT_RECONNECT_BACKOFF_S=2.0` mit Faktor 2.0 und
3 Versuchen. Anpassung in AP-11:

- Cap auf 30 s.
- Versuche unbegrenzt, solange `AuthLifecycle` aktiv ist; nur
  Auth-Verlust beendet die Loop.
- Backoff-Status (`reconnect_attempt`, nächste Wartezeit) im
  `/v1/status`-Endpoint.

### 4.4 Backpressure und SLO-Verletzung

- **Adapter → StreamHub:** unbounded asyncio-Queue ist riskant; Limit
  pro Topic-Adapter (z. B. 1024 Frames) mit Drop-Oldest und Counter-
  Metric.
- **StreamHub → Consumer:** Slow-Consumer-Drop pro Egress-Verbindung
  (separate AP-11-Karte, weil nicht trivial). Der Refcount-Slot
  selbst bleibt aktiv, solange mindestens ein Consumer mitkommt.
- **SLO-Logging:** Adapter mißt End-to-End (CP-Receive → Egress) und
  logt ab Robot-Schwelle (p95>150 ms `smd`, p95>250 ms `sor`).
  Status-Frame `slo_breach: { topic, percentile, value_ms }` im
  Stream — Robot reagiert intern (Risk-Rule), PSM zeigt Indicator.

---

## 5. Handelszeit, Tradeability und Börsenkalender (neu in Rev. 2)

### 5.1 IBKR-Quellen

| Endpoint / Feld | Inhalt | Cache |
|-----------------|--------|-------|
| `GET /trsrv/secdef/schedule?assetClass=STK&symbol=<sym>&exchange=<exch>` | `tradingScheduleList[]` mit `sessions[]` (`openingTime`, `closingTime`, `prop=LIQUID|NON_LIQUID`) für 14 Tage. Feiertage = Tag mit leerer `sessions`-Liste oder fehlender Eintrag. | `CalendarService`, pro `exchange_id`, TTL 12h |
| `GET /iserver/contract/{conid}/info` | `listingExchange`/`primaryExchange`, Feld `tradingHours` als String | Pro `conid`, TTL 24h |
| `smd`-Frame-Felder | `6509` (Availability-Code: R/D/H/Z/...), `7295` (Trading-Hours-Hint), `7296` (Liquid-Hours-Hint) — nur im **ersten** Frame nach Subscribe vollständig, in Folge-Frames Delta-only | Pro Subscription im Topic-Adapter |

### 5.2 Adapter-Logik

`CalendarService.get(exchange_id)` ist der einzige Zugriffspunkt auf
den Schedule. Cache-Miss → REST-Call gegen CP-Gateway, Cache-Hit →
sofort. `TopicAdapter "smd"` ruft den Service einmal pro neuem
`conid` auf (über `Symbol→Börse`-Mapping aus `instruments`-Cache),
hält das Ergebnis in der Subscription und kombiniert es mit dem
Live-`availability_code` zu `is_tradeable_now`/`current_session`.

`is_tradeable_now`-Wahrheits-Tabelle:

| `current_session` | `availability_code` | `is_tradeable_now` |
|-------------------|---------------------|---------------------|
| `rth` | R / D | **true** |
| `pre` | R / D | **true** |
| `post` | R / D | **true** |
| `closed` | egal | false |
| egal | H (Halted) / Z (Zero-Volume-Halt) | false |

Werte für `availability_code` siehe Auto-Memory *IBKR Feld 6509
Availability-Code* — der Adapter sollte das vollständige Mapping in
`docs/06-glossary.md` ausbauen, sobald die Implementierung steht.

### 5.3 Consumer-Endpunkte für Kalender

- **`GET /v1/exchanges`** — Liste aller bisher gesehenen Börsen mit
  `exchange_id`, `description`, `time_zone`, Anzahl gecachter
  Schedules.
- **`GET /v1/exchanges/{exchange_id}/calendar?days=14`** —
  börsenzentrierter Schedule. Default `days=14`, akzeptierter Bereich
  `1..14`. Antwort:

```json
{
  "exchange_id": "NASDAQ",
  "time_zone": "America/New_York",
  "days": [
    {
      "date": "2026-05-01",
      "is_holiday": false,
      "sessions": [
        {"type": "pre",  "opens_at": "2026-05-01T04:00:00-04:00",
         "closes_at": "2026-05-01T09:30:00-04:00"},
        {"type": "rth",  "opens_at": "2026-05-01T09:30:00-04:00",
         "closes_at": "2026-05-01T16:00:00-04:00"},
        {"type": "post", "opens_at": "2026-05-01T16:00:00-04:00",
         "closes_at": "2026-05-01T20:00:00-04:00"}
      ]
    },
    {
      "date": "2026-05-25",
      "is_holiday": true,
      "sessions": []
    }
  ]
}
```

- **`GET /v1/instruments/{conid}`** wird um `exchange_id` und einen
  Convenience-Link `calendar_url: "/v1/exchanges/<id>/calendar"`
  ergänzt.

### 5.4 Konsumenten-Nutzung

- **trading_robot:** nutzt `is_tradeable_now` aus dem `smd`-Frame in
  der Risk-Rule für Entry-Entscheidungen; nutzt
  `/v1/exchanges/{id}/calendar` einmal pro Session-Start, um
  Trading-Window für den Tag zu plotten.
- **PSM:** nutzt `current_session` und `is_tradeable_now` für UI-
  Anzeige („Markt geschlossen, öffnet morgen 09:30"); nutzt
  `/v1/exchanges/{id}/calendar` für eine Wochen-/Feiertags-Übersicht.

---

## 6. Migration und Backwards-Compat

### 6.1 Phase-Plan

| Phase | Ziel | Garantien für Consumer |
|-------|------|------------------------|
| Heute | REST-Polling-`smd` + REST-`sor` | Frames identisch, keine Latenz-Garantie unter 1 s |
| Phase A | `smd`-WS-Push aktiv über bestehenden SSE-Pfad. Tradeability-Felder im Frame. Calendar-Endpoint live. | `/v1/quotes/stream`-Vertrag identisch (additiv: neue Felder), Latenz sinkt |
| Phase B (parallel) | `sor`-WS-Push aktiv (`/v1/orders/stream` SSE neu). WS-Egress-Endpunkte `/v1/quotes/ws` und `/v1/orders/ws` parallel zu SSE. | Endpunkte additiv; bestehende Order-REST bleibt; Consumer wählt SSE oder WS pro Verbindung |

Phase A ist **die** Pflicht-Lieferung; Phase B kann zeitlich später,
wenn Phase A im Live-Konto sauber läuft.

### 6.2 Rollback-Pfad

- **Per-Endpoint-ENV-Schalter** (`BG_QUOTES_SOURCE=ws|polling`,
  `BG_ORDERS_SOURCE=ws|rest`) erlaubt schnellen Rückfall auf den
  Polling-Pfad ohne Code-Revert. Schalter steht in `.env.live` und
  `.env.paper` separat.
- Polling-Quelle bleibt im Code aktiv, bis WS-Source mindestens einen
  ganzen Trading-Tag im Live-Konto unterbrechungsfrei läuft.

### 6.3 Schema-Wechsel-Praxis

Statt eines formalen N-1-Compat-Vertrags: **synchrone Schema-Updates
in beidseitiger Koordination.** Wenn ein Frame-Feld umbenannt wird,
geschieht das in einem Patch-Bump pro Repo (broker-gateway, PSM,
trading_robot) hintereinander, gefolgt von gemeinsamem Deploy.
Möglich, weil alle drei Repos unter derselben User-Kontrolle stehen.

Optionales `schema_version: int`-Frame-Feld bleibt als Diagnose-Hilfe
verfügbar — nicht als Vertragspflicht.

---

## 7. Folge-AP — vorgeschlagene Karten-Skizzen

> Verbindlich zur Anlage erst nach User-OK in einem getrennten
> `outline`-Lauf. Vorschlag-Titel des Folge-AP: **„AP-11 WS-Adapter
> Implementation"**. Reihenfolge entspricht Bauphase A → B; Karten
> 7–8 sind Phase-B oder querschnittlich.

| # | Titel-Skizze | Soll-Zustand (1 Satz) | Phase |
|---|--------------|-----------------------|-------|
| 1 | TopicAdapter `smd`: Frame-Parser + semantisches Mapping | Mixed-type-Felder dekodiert, Delta-zu-Snapshot gemerged, `tic`-/Doppel-Frame-Dedup, Unit-Tests gegen K4-Recordings grün. | A |
| 2 | SubscriptionRegistry mit Replay nach `connect()` | Soll-State aller aktiven Subscriptions persistiert in-memory, Replay nach jedem Reconnect, Auth-Loss pausiert. | A |
| 3 | WSPushSource für StreamHub + ENV-Schalter `BG_QUOTES_SOURCE=ws\|polling` | WS-Push als 1st-class Quelle für `/v1/quotes/stream`, Polling-Quelle bleibt opt-in als Fallback; Vertrag des bestehenden SSE-Endpunkts unverändert. | A |
| 4 | CalendarService + `/v1/exchanges/{id}/calendar` + Symbol→Börse-Mapping | 12h-Cache pro `exchange_id` aus `/trsrv/secdef/schedule`, Endpoint mit `?days=N` (Default 14, max 14), `/v1/instruments/{conid}` um `exchange_id` und `calendar_url` ergänzt. | A |
| 5 | Tradeability-Felder im `smd`-Frame | `is_tradeable_now: bool` und `current_session` aus Live-`availability_code` + `CalendarService` abgeleitet, im Frame zusätzlich zu Quote-Feldern. | A |
| 6 | TopicAdapter `sor` + REST-Bootstrap + `/v1/orders/stream` | `sor`-Frames in semantische Order-Snapshots normalisiert, `timeInForce`-Quirk gemappt, Bootstrap-Frame zuerst, dann Live; Endpoint analog zu `quotes_stream` mit Scope `read:streams.orders`. | B |
| 7 | WS-Egress-Endpunkte `/v1/quotes/ws` + `/v1/orders/ws` | Parallel zu SSE, identisches Frame-Format, derselbe StreamHub als Quelle, Bearer-Token via `Sec-WebSocket-Protocol` oder `?token=`, `last_event_id`-Replay-Param. | B |
| 8 | `/v1/status` Endpoint + 150-Symbol-`smd`-Stresstest | Felder `cp_gateway_connected`, `last_frame_age_seconds`, `reconnect_attempt`, `subscriptions_active`; Live-Stresstest gegen U25235077 mit 150 Symbolen für PSM-Worst-Case-Skala, Pacing-Beobachtung, Backpressure-Strategie pro Topic-Adapter. | A oder B |

---

## 8. Risiken und offene Fragen

### 8.1 Zur User-Klärung vor AP-11-Anlage

Alle vier offenen Punkte aus Rev. 1 sind durch das User-Review
**aufgelöst**:

| # | Punkt aus Rev. 1 | Rev.-2-Auflösung |
|---|------------------|------------------|
| 1 | Multi-Tenant | Single-Tenant zementiert; PSM bleibt Single-User. |
| 2 | WS-Egress | Ja, parallel zu SSE als Phase B. |
| 3 | Per-Konto-Token-Scopes | Nein, bleibt Coarse-Scope. |
| 4 | Schema-Versionierungs-Vertrag | Nein, synchron koordiniert; `schema_version` nur als optionales Diagnose-Feld. |

Verbleibender Klärungspunkt: nichts für AP-11-Anlage, nur die
Benennung des Folge-AP („AP-11 WS-Adapter Implementation" als
Vorschlag).

### 8.2 Technische Risiken

- **K4-`str`-Re-Send-Quirk**: 277 Frames in 60 s für einen Vortags-
  Trade ist nicht erklärt. Da `str` nicht integriert wird, ist das
  Risiko deferred — bei späterer Integration ist eine eigene
  Recherche-Karte Pflicht.
- **`tic`-4er-Multiplikator**: K3-Client deduppt schon, aber das
  Verhalten ist undokumentiert; bei Major-CP-Gateway-Update neu
  verifizieren.
- **Initial-Snapshot-`sor`-Lücke**: K4 hat keinen Snapshot bei Sub
  ohne offene Orders gesehen — REST-Bootstrap ist Pflicht. Wenn
  IBKR das Verhalten ändert, wird der Bootstrap redundant; nicht
  schlimm, aber im Adapter-Doku zu erwähnen.
- **Latenz-SLO Robot p95<150 ms**: Adapter-intern und CP-Gateway-
  intern gibt es noch ungemessene Anteile. Nach Phase A empirisch
  messen; Karte 8 enthält den Stresstest-Bezug.
- **Backpressure**: Slow-Consumer kann den ganzen Stream-Pfad
  ausbremsen. Drop-Oldest in Karte 3, Slow-Consumer-Drop in
  separater Karte.
- **Calendar-Cache-Invalidation**: 12h-TTL ist konservativ, IBKR
  ändert Schedule praktisch nie unterjährig. Risiko: an
  Halbtages-Sessions (z.B. Tag vor US-Feiertag) wird der Cache zu
  spät invalidiert. Mitigation: Cache-Refresh expliziter Trigger im
  Status-Endpoint (`POST /v1/admin/refresh-calendars`) — kann eine
  separate Karte werden, wenn nötig.
- **WS-Egress-Auth**: `Sec-WebSocket-Protocol`-Header-Parsing ist
  erfahrungsgemäß rough. Falls Probleme auftreten, Query-Param-Auth
  als Fallback ist im Doku schon vorgesehen.

### 8.3 Konsumenten-Konflikt-Auflösungen

| Punkt | PSM | trading_robot | Adapter-Wahl |
|-------|-----|---------------|--------------|
| Failure-Mode | REST-Polling als eigene Reaktion | Fail-Loud | `fail-loud` global, PSM macht REST-Polling im eigenen Backend |
| Latenz-SLO `smd` p95 | < 1500 ms | < 150 ms | Robot-SLO als Adapter-Ziel |
| Skala `smd` | 150 Symbole | 30 Symbole | 150-Cap, mit Stresstest |
| Frame-Schema | semantisch + Voll-Snapshot | semantisch + Voll-Snapshot | identisch übernommen |
| Transport | SSE (Phase A) + offen für WS | SSE | SSE Pflicht-Pfad, WS parallel als Ziel-Tier |
| Schema-Versionierung | optional Pflichtfeld + N-1-Compat | Pflichtfeld | optional, synchron koordiniert (User-Direktive) |

---

## 9. Decision-Gate

**Decision-Gate ist freigegeben.** Folge-Schritt: `outline`-Lauf für
das Folge-AP („AP-11 WS-Adapter Implementation") mit den 8 in
Sektion 7 vorgeschlagenen Karten-Skizzen.

Dieses Dokument bleibt der verbindliche Architektur-Schnitt für die
WS-Adapter-Implementation und wird **nicht** je AP-11-Karte mit-
versioniert. Substanzielle Architektur-Änderungen während der
Implementation werden hier als Rev.-N-Block ergänzt; punktuelle
Detail-Klärungen wandern in die jeweilige AP-11-Karte.

---

## Anhang A — `sor`-Frame-Felder (semantisch)

| Adapter-Feld | IBKR-Quelle (K4) | Bemerkung |
|--------------|------------------|-----------|
| `order_id` | `orderId` | int |
| `client_order_id` | `cOID` (cp) / `orderRef` (tws) | Korrelationsschlüssel des Aufrufers, **kein** Idempotency-Schlüssel — siehe Hinweis unter der Tabelle |
| `parent_id` | `parentId` | für Brackets |
| `account` | `acct` | |
| `symbol` | `ticker` | |
| `side` | `side` | `BUY`/`SELL` |
| `quantity` | `totalSize` | Decimal |
| `filled_quantity` | `filledQuantity` | Decimal |
| `avg_fill_price` | `avgPrice` | Decimal |
| `status` | `status` | `pending`/`accepted`/`partial_fill`/`filled`/`cancelled`/`rejected` (mapped) |
| `time_in_force` | `timeInForce` | normalisiert (`CLOSE` → `DAY` außerhalb RTH, etc.) |
| `last_event_at` | `lastExecutionTime` | ISO-8601 |
| `reject_reason` | `orderRejectReason` (falls vorhanden) | |

UI-only Felder (`bgColor`, `fgColor`) werden **nicht** durchgereicht.

> **Zu `client_order_id` (Karte `0cfea205`, korrigiert 2026-07-16).** Dieses Dokument führte das Feld als „Idempotency-Schlüssel". Das galt aus der cp-Perspektive, in der es auf `cOID` abbildet — dort prüft der Broker die Eindeutigkeit. Für das TWS-Backend, das den Pfad seit v2.0.0 trägt, stimmt es nicht: dort bildet das Feld auf `orderRef` ab, und IBKR erzwingt darauf **keine** Eindeutigkeit. Gegen Paper gemessen: zwei Orders mit identischem `orderRef` werden beide angenommen und bekommen verschiedene `permId`s. Das Feld korreliert eine Order über ihren Lebenszyklus (die `order_id` wechselt von `orderId` auf `permId` und taugt dafür nicht); gegen Doppel-Submit schützt allein der `Idempotency-Key`-Header. Details in [`docs/api/v1.md`](../api/v1.md) Section 7.1.

---

## Anhang B — `smd`-Frame-Felder (semantisch)

| Adapter-Feld | IBKR-Field-ID | Typ am Egress |
|--------------|----------------|---------------|
| `last` | 31 | Decimal |
| `bid` | 84 | Decimal |
| `ask` | 86 | Decimal |
| `bid_size` | 88 | int |
| `ask_size` | 85 | int |
| `volume` | 87 | int |
| `last_size` | 7059 | int |
| `change_pct` | 83 | float |
| `availability_code` | 6509 | string (`R`/`D`/`H`/`Z`/...) |
| `high` | 70 | Decimal |
| `low` | 71 | Decimal |
| `server_id` | `server_id`/6119 | string (informational) |
| `conid` | `conid` | int |
| `exchange_id` | abgeleitet aus `instruments`-Cache | string |
| `is_tradeable_now` | abgeleitet | bool |
| `current_session` | abgeleitet | `"rth"\|"pre"\|"post"\|"closed"\|"halted"` |
| `updated_at` | `_updated` | ISO-8601 |

Adapter merged Delta-Frames in den letzten Snapshot-Zustand, sodass
am Egress immer ein Voll-Snapshot pro Frame ankommt.

---

## Anhang C — Handelszeit, Tradeability und Börsenkalender

### C.1 Drei-Schichten-Modell

| Schicht | Quelle | Persistenz | Wer nutzt |
|---------|--------|------------|-----------|
| **C.1.1 Börsenkalender** (14 Tage Sessions) | `/trsrv/secdef/schedule` | `CalendarService`, pro `exchange_id`, TTL 12h | Calendar-Endpoint, Tradeability-Ableitung im Adapter |
| **C.1.2 Symbol→Börse-Mapping** | `/iserver/contract/{conid}/info`, Feld `listingExchange` | `instruments`-Cache, pro `conid`, TTL 24h | Tradeability-Ableitung (`smd`-Adapter braucht `exchange_id`) |
| **C.1.3 Live-Tradeability** (jetzt offen / Halt / Pre / Post) | `smd`-Frame-Felder `6509`, `7295`, `7296` | Stream-only, kein eigener Cache | Frame-Felder `is_tradeable_now` + `current_session` |

### C.2 Calendar-Endpoint-Vertrag

`GET /v1/exchanges/{exchange_id}/calendar?days=N`

- **Default `days=14`**, akzeptierter Bereich `1..14` (IBKR liefert
  praktisch immer 14, mehr ist nicht garantiert verfügbar).
- Antwort enthält pro Tag: `date` (ISO-Datum, Börsen-Lokalzeit),
  `is_holiday: bool`, `sessions: [{type: rth|pre|post, opens_at,
  closes_at}]`.
- Feiertage = Tag mit `is_holiday=true` und leerem `sessions`-Array.
- Halbtages-Sessions werden als verkürzte `rth`-Session
  abgebildet — IBKR liefert das nativ als kürzeren `closingTime`.

### C.3 IBKR-Schedule-Quirks

- `prop=LIQUID` ↔ RTH; `prop=NON_LIQUID` ↔ Pre oder Post (Adapter
  unterscheidet anhand der Uhrzeit relativ zur RTH-Session).
- Manche Symbole (z.B. ETFs) haben an manchen Tagen nur eine
  einzelne LIQUID-Session ohne Pre/Post — der Endpoint reflektiert
  das 1:1.
- Time-Zone kommt aus dem Schedule-Response selbst (Feld
  `timeZoneId`), nicht aus einem separaten Endpoint.

### C.4 Beispiel: NASDAQ-Wochenkalender

```
GET /v1/exchanges/NASDAQ/calendar?days=7
```

```json
{
  "exchange_id": "NASDAQ",
  "time_zone": "America/New_York",
  "days": [
    {"date": "2026-05-01", "is_holiday": false,
     "sessions": [
       {"type": "pre",  "opens_at": "2026-05-01T04:00:00-04:00",
        "closes_at": "2026-05-01T09:30:00-04:00"},
       {"type": "rth",  "opens_at": "2026-05-01T09:30:00-04:00",
        "closes_at": "2026-05-01T16:00:00-04:00"},
       {"type": "post", "opens_at": "2026-05-01T16:00:00-04:00",
        "closes_at": "2026-05-01T20:00:00-04:00"}
     ]},
    {"date": "2026-05-02", "is_holiday": true,  "sessions": []},
    {"date": "2026-05-03", "is_holiday": true,  "sessions": []},
    {"date": "2026-05-04", "is_holiday": false, "sessions": [...]},
    {"date": "2026-05-05", "is_holiday": false, "sessions": [...]},
    {"date": "2026-05-06", "is_holiday": false, "sessions": [...]},
    {"date": "2026-05-07", "is_holiday": false, "sessions": [...]}
  ]
}
```
