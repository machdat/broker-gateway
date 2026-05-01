# WS-Adapter-Architektur-Design (AP-04 K6, Decision-Gate AP-04 → AP-05)

**Stand:** 2026-05-01 · **Verantwortlich:** Christian Mangold ·
**Status:** Decision-Gate — bei Freigabe Anlage AP-05 mit den unten
skizzierten Karten in einem getrennten `outline`-Lauf.

> **Was diese Datei ist.** Konsolidierter Architektur-Schnitt für den
> produktiven WebSocket-Adapter zwischen `CPWebSocketClient` (K3) und
> den Consumern (PSM, trading_robot). Sie verdichtet drei Eingaben:
> die Phase-1-Findings aus K1–K4, die Consumer-Antworten aus den
> beiden Sibling-Projekten und die Bestandsarchitektur des Service.
> Sie ersetzt **kein** Implementations-Detail — die Karten in AP-05
> entscheiden Schnitt und Reihenfolge der Umsetzung.
>
> **Was diese Datei nicht ist.** Kein Production-Code, keine
> verbindliche Karten-Anlage. AP-05-Skizzen in Sektion 7 sind
> Vorschläge, die explizit User-Freigabe brauchen.

---

## 0. Quellen

| Quelle | Pfad / Karte | Inhalt |
|--------|--------------|--------|
| Phase-1-Findings (K1) | [`docs/research/ibkr-cpapi-websockets-findings.md`](../research/ibkr-cpapi-websockets-findings.md) | Setup-Realität, Frame-Format, Heartbeat, `tic`-Quirk |
| Recording-Schema (K2) | `tests/cp_mock/ws_replay.py`, `tests/fixtures/recorded/ws/spike-baseline.jsonl` | JSONL-Format für Replay |
| WS-Client (K3) | `src/broker_gateway/cp/ws_client.py` | Connect, Auth, `tic`-Loop, Reconnect mit Backoff |
| Topic-Exploration (K4) | dieselbe Findings-Datei, Sektion *Topic-Exploration* | smd/sor/str-Verhalten, Reife-Ranking |
| K5 Fragenkatalog | [`docs/architecture/ws-consumer-questions.md`](ws-consumer-questions.md) | Reiner Katalog, Antworten in Consumer-Repos |
| PSM-Antwort | `personal_stock_manager/docs/integrations/broker-gateway.md` (PSM-Repo, KanPrompt-Karte `a1037c45-b4af-42bc-abd4-8a2ac015ceeb`) | UI-getrieben, REST-Fallback für `smd`, 150-Symbol-Cap |
| trading_robot-Antwort | `trading_robot/docs/integrations/broker-gateway.md` (trading_robot-Repo, KanPrompt-Karte `e71623d2-bd8d-4643-a15b-6d93c1afafd5`) | Trading-kritisch, Fail-Loud, 30-Symbol-Cap, p95<150 ms |

Bestandscode-Bezug: `streams/manager.py` (SubscriptionManager mit
Refcount und Fan-Out, heute Polling-basiert), `cp/lifecycle.py`
(AuthLifecycle, Heartbeat-/Reauth-Loop), `api/v1/quotes_stream.py`
(SSE-Endpoint `/v1/quotes/stream`), `api/v1/events_stream.py`
(SSE-Endpoint `/v1/events`).

---

## 1. Zielbild

### 1.1 Topic-Entscheidungen

| Topic | Status in AP-05 | Begründung |
|-------|-----------------|------------|
| `smd` (Market-Data Snapshot/Delta) | **WS-integriert** | Beide Consumer brauchen es; K4-Reife grün; Refcount-Layer existiert bereits. Erste Iteration zielt nur auf `smd`. |
| `sor` (Order-Lifecycle) | **WS-integriert (zweite Iteration)** | Beide Consumer brauchen es; trading_robot blockiert Live-Trade ohne `sor`-Push; REST-Bootstrap (`/iserver/account/orders`) ist Pflicht-Vorlauf, weil IBKR keinen Initial-Snapshot über WS garantiert. |
| `str` (Trades / Executions) | **REST-Polling, kein Push** | trading_robot lehnt Push für v1 ab (Re-Send-Quirk, `execution_id`-Dedup-Pflicht), PSM nimmt es nur optional. Bestehender Trades-Endpunkt bleibt. |
| `spl` (Profit/Loss) | **Nicht integriert** | Beide Consumer rechnen P/L selbst; zweite Wahrheit ist SSOT-Verstoß. |
| `smh` (Market-Data History) | **Nicht integriert** | Historische Daten kommen aus REST/PSM bzw. externen Quellen. |
| `sbd` (Bulletin) | **Nicht integriert** | Kein Bot-Logik-Input, kein UI-Hook. |

### 1.2 Reihenfolge-Bedingungen

- `smd`-WS-Push muss vor dem ersten Paper-Trading-Test im
  trading_robot-Repo stehen (sonst kein verlässlicher Live-Quote-Pfad
  für den Paper-Modus).
- `sor`-WS-Push muss vor dem ersten echten Live-Trade stehen.
- AP-05 darf in mehreren Iterationen laufen — `smd` zuerst, `sor`
  später; PSM hat **kein** hartes Datum (Q3-2026 frühestens, nach
  PSM-M2-Karte `225`).

### 1.3 Was bewusst draußen bleibt

- WebSocket-Egress vom Service zu Consumern. Beide Consumer wählen
  SSE; trading_robot lehnt WS-Egress ausdrücklich ab. Wird in AP-05
  nicht implementiert; offenes Feld für PSM siehe Sektion 8.
- Multi-Tenant-Adapter (mehrere PSM-Endkunden auf einer
  broker-gateway-Instanz). Initial Single-Tenant; Erweiterung erst
  nach PSM-M2.
- Gemultiplexer Single-Stream (alle Topics auf einer SSE-Connection).
  Beide Consumer kommen mit getrennten Streams aus, eine Multiplex-
  Schicht würde nur Komplexität ohne Mehrwert hinzufügen.

---

## 2. Komponenten-Diagramm

```
+---------------------------+        +---------------------------+
| trading_robot             |        | personal_stock_manager    |
|   MarketDataPort-Adapter  |        |   Quotes-Daemon           |
|   BrokerPort-Adapter      |        |   Order-View              |
+-------------+-------------+        +-------------+-------------+
              |  SSE (Last-Event-ID, Bearer-Token)              |
              v                                                 v
+-----------------------------------------------------------------+
| broker-gateway · /v1                                            |
|                                                                 |
|  +------------------+     +-------------------+                 |
|  | quotes_stream    |     | orders_stream     |  (neu in AP-05) |
|  | /v1/quotes/stream|     | /v1/orders/stream |                 |
|  +---------+--------+     +---------+---------+                 |
|            |                        |                           |
|            v                        v                           |
|  +-----------------------------------------------------------+  |
|  | SubscriptionManager (Refcount + Fan-Out, heute Polling)   |  |
|  |   - aktiv pro Symbol/Konto, dedupliziert Consumer         |  |
|  |   - Quelle ist heute REST-Polling, neu: WS-Push als 1st   |  |
|  +---------+----------------------------+----------------+----+ |
|            ^                            ^                ^      |
|  +---------+-----------+   +------------+----------+     |      |
|  | TopicAdapter "smd"  |   | TopicAdapter "sor"    |     |      |
|  |   - Frame-Parser    |   |   - Lifecycle-Mapper  |     |      |
|  |   - Field-Decoder   |   |   - Bootstrap-Glue    |     |      |
|  |   - Snapshot-Merge  |   |     (REST-Initial)    |     |      |
|  +---------+-----------+   +------------+----------+     |      |
|            |                            |                |      |
|            +-------------+--------------+                |      |
|                          v                               |      |
|              +---------------------+                     |      |
|              | SubscriptionRegistry|  ----- Replay nach Reconnect
|              |   - Soll-State je   |                     |      |
|              |     Topic+Args      |                     |      |
|              |   - re-applies on   |                     |      |
|              |     CPWebSocketClient.connect()           |      |
|              +----------+----------+                     |      |
|                         |                                |      |
|                         v                                |      |
|              +---------------------+                     |      |
|              | CPWebSocketClient   |  (K3, Bestand)      |      |
|              |   connect/auth/tic  |                     |      |
|              |   reconnect+backoff |                     |      |
|              +----------+----------+                     |      |
|                         |                                |      |
|                         v                                |      |
|       +--------------------------------+                 |      |
|       | AuthLifecycle (cp/lifecycle.py)|                 |      |
|       | Heartbeat-Tick + Session-Check |                 |      |
|       +----------------+---------------+                 |      |
|                        |                                 |      |
|                        v                                 |      |
|                +-------+-------+    +--------------------+----+ |
|                |  CP-Gateway   |    | StatusEndpoint /v1/status| |
|                |  (intern,     |    |  cp_connected, last_frame_| |
|                |   Compose)    |    |  age, reconnect_attempt   | |
|                +---------------+    +---------------------------+ |
+-----------------------------------------------------------------+
```

### 2.1 Neue Layer (gegenüber heute)

| Layer | Zweck | Wo | Bestand? |
|-------|-------|----|----------|
| `SubscriptionRegistry` | Hält den **Soll-State** aller aktiven Subscriptions (Topic + Args + Consumer-Refs). Replayt nach `CPWebSocketClient.connect()` alle Subscribes erneut, weil der Server den State nicht persistiert (K4-Reconnect-Befund). | `streams/registry.py` (neu) | nein |
| `TopicAdapter` (pro Topic) | Parsed Frame-Schema (mixed types: Strings für Preise, Floats für %-Change), normalisiert Felder auf semantische Namen, deduppt `tic`/`smd`-Doppel-Frames, baut **Voll-Snapshot pro Frame** aus Delta-Updates. | `cp/topics/smd.py`, `cp/topics/sor.py` (neu) | nein |
| `WSPushSource` für `SubscriptionManager` | Neue Quelle neben dem Polling-Pfad; abonniert beim Topic-Adapter, schiebt Frames in den vorhandenen Refcount-/Fan-Out-Mechanismus. | `streams/ws_source.py` (neu) | nein |
| `OrdersStreamRouter` (`/v1/orders/stream`) | Neuer SSE-Endpoint, analog zu `quotes_stream.py`. Initialer REST-Bootstrap via existierenden CP-REST-Client, dann Live-Frames aus `sor`-TopicAdapter. | `api/v1/orders_stream.py` (neu) | nein |
| `StatusEndpoint` (`/v1/status`) | Beobachter-API. Felder: `cp_gateway_connected`, `last_frame_age_seconds`, `reconnect_attempt`, `subscriptions_active`. PSM polled 10 s, trading_robot konsumiert vor jeder Order-Entscheidung. | `api/v1/status.py` (neu) | nein |

### 2.2 Beziehung zu `AuthLifecycle`

`CPWebSocketClient` läuft **parallel** zum bestehenden
`AuthLifecycle`-Tick (REST-Heartbeat alle 60 s). Auth-Verlust an einer
der beiden Stellen ist Auth-Verlust für beide:

- WS-seitig: `sts.authenticated=false` → Adapter triggert
  `AuthLifecycle.reauthenticate(force=True)` und schließt den
  WS-Stream. Reconnect erfolgt nach erfolgreichem Reauth-Tick.
- REST-seitig: `AuthLifecycle` detektiert Auth-Loss → Signal an den
  `SubscriptionRegistry`, alle Subscriptions zu pausieren, bis
  `AuthStatus == AUTHENTICATED` zurück ist; Resubscribe danach.

Der bestehende `cp/lifecycle.py::reauthenticate(force=True)`-Pfad
bleibt das einzige Reauth-Tor. Der WS-Adapter ist Konsument der
Lifecycle, nicht Co-Owner.

---

## 3. Consumer-API

### 3.1 Bestand bleibt

`/v1/quotes/stream` (SSE) bleibt **unverändert** im Vertrag (Pfad,
Felder, `Last-Event-ID`-Semantik). Nur die *Quelle* wechselt: bisher
REST-Polling pro `conid`, neu WS-Push aus dem `smd`-TopicAdapter.

| Garantie | Heute | Nach AP-05 |
|----------|-------|------------|
| SSE-Pfad | `/v1/quotes/stream?conids=…` | unverändert |
| Frame-Format | semantische Felder | unverändert (gleicher Snapshot) |
| `Last-Event-ID`-Reconnect | ja | ja |
| Liveness | SSE-Comment-Heartbeat | unverändert (15 s, siehe Sektion 5.3) |
| Latenz `smd` p95 | ~1 s (Polling-Takt) | < 150 ms (Robot-SLO) |

### 3.2 Neu: `/v1/orders/stream`

SSE-Endpoint, identisches Pattern zu `quotes_stream`:

- **Method:** `GET /v1/orders/stream` (Bearer-Token mit
  `read:streams.orders`).
- **Query:** `account=<id>` (Pflicht), optional `event_types=` (Filter
  wie bei `/v1/events`).
- **Frame:** vollständiger Order-Snapshot pro Status-Wechsel
  (PSM- und Robot-übereinstimmend), Felder siehe Anhang A.
- **Bootstrap:** der Endpoint **erst** den initialen REST-Snapshot
  via `GET /iserver/account/orders` (CP-REST-Client) als ersten Frame
  ausliefern, dann auf den Live-Push umschalten. So ist die
  Robot-Erwartung „kein Lücken zwischen Subscribe und erstem Frame"
  erfüllt; PSM bekommt denselben Bootstrap geschenkt.

### 3.3 Frame-Schema-Versionierung

- **Pflichtfeld** `schema_version: int` in jedem `data:`-Frame.
- **Header** `X-Schema-Version: <int>` (Subscribe-Header) optional;
  Adapter kann es nutzen, um einer älteren Consumer-Version
  N-1-Schema-Compat zu liefern. PSM fordert N-1 mindestens einen
  Release-Cycle, trading_robot lehnt unbekannte Major-Versionen ab.
- Initial: `schema_version=1`. Major-Bumps gehen mit einem ADR-Eintrag
  in `docs/architecture/` einher.

### 3.4 Failure-Mode pro Subscription

Konflikt-Auflösung zwischen den Consumern (PSM will REST-Fallback für
`smd`, trading_robot will Fail-Loud) per Subscription-Header:

```
X-Stream-Failure-Mode: fail-loud | rest-fallback | silent-skip
```

Default ohne Header: `fail-loud` (sicherer Default; explizite Wahl
gewünscht).

| Wert | Verhalten | Wer wählt das |
|------|-----------|---------------|
| `fail-loud` | Stream-Close mit Error-Frame, keine Daten mehr; Consumer reconnectet | trading_robot für `smd`/`sor`; PSM für `sor` |
| `rest-fallback` | Adapter pollt REST und liefert dieselben Frames mit höherer Latenz weiter; Status-Frame `degraded=rest-fallback` im Stream | PSM für `smd` |
| `silent-skip` | Adapter dropt Frames stillschweigend (nur loggt); Stream bleibt offen | PSM für `str`-Push (falls jemals integriert) |

`silent-skip` ist nur als Per-Subscription-Wahl verfügbar, **nie**
als Adapter-Default. trading_robot lehnt `silent-skip` für alle
relevanten Topics ab.

### 3.5 Authentifizierung & Scopes

Bestand `docs/04-security.md` bleibt:

| Scope | Topic / Pfad |
|-------|--------------|
| `read:streams.quotes` | `/v1/quotes/stream` (`smd`) |
| `read:streams.orders` | `/v1/orders/stream` (`sor`) |
| `read:orders` | REST-Bootstrap, REST-Reconcile |
| `write:orders` | bestehender Order-Submit-Pfad |
| `read:status` | `/v1/status` |

Granularität pro Konto bleibt offene Erweiterung (siehe Sektion 8).

---

## 4. Failure-Mode-Strategie

### 4.1 WS-Abbruch (CP-Gateway erreichbar, WS tot)

`CPWebSocketClient` hat schon Reconnect mit Backoff (Bestand). Neu
hinzu kommt:

1. **`SubscriptionRegistry.replay()`** nach jedem erfolgreichen
   `connect()` — alle aktiven Subscribes neu absetzen.
2. **Status-Frame** `subscription_replay_in_progress` an alle
   abhängigen SSE-Streams, bis Replay durch ist.
3. **Latenz-Spike-Toleranz** — der erste Frame nach Reconnect kann
   merklich später kommen; Status-Endpoint zeigt Reconnect-Phase.

### 4.2 CP-Gateway-Restart

Operativ wie WS-Abbruch plus REST-Reauth-Bedarf:

1. WS-Reader sieht Disconnect.
2. `AuthLifecycle.reauthenticate(force=True)` (entspricht der
   Pause-Wiederaufnahme-Sequenz aus der Auto-Memory).
3. Sobald `AuthStatus == AUTHENTICATED`, `CPWebSocketClient.connect()`
   neu, `SubscriptionRegistry.replay()`.
4. Optional `gateway_restarting`-Status-Frame im Stream, damit der
   Robot-Risk-Layer dieses Fenster ohne Pager-Alarm tolerieren kann
   (Robot-§4.2).

### 4.3 Reconnect-Backoff (Adapter ↔ CP-Gateway)

Bestand: `_DEFAULT_RECONNECT_BACKOFF_S=2.0` mit Faktor 2.0 und
3 Versuchen. Anpassung in AP-05:

- Cap auf 30 s (PSM-Erwartung).
- Versuche unbegrenzt, solange `AuthLifecycle` aktiv ist; nur
  Auth-Verlust beendet die Loop.
- Backoff-Status (`reconnect_attempt`, nächste Wartezeit) im
  `/v1/status`-Endpoint.

### 4.4 Backpressure und SLO-Verletzung

- **Adapter → SubscriptionManager:** unbounded asyncio-Queue ist
  riskant; Limit pro Topic-Adapter (z. B. 1024 Frames) mit
  Drop-Oldest und Counter-Metric.
- **SubscriptionManager → SSE-Consumer:** bestehender Refcount-/
  Fan-Out-Pfad; Backpressure pro Consumer durch Slow-Consumer-Drop
  (separate AP-05-Karte, weil nicht trivial).
- **SLO-Logging:** Adapter mißt End-to-End (CP-Receive → SSE-Egress)
  und logt ab Robot-Schwelle (p95>150 ms `smd`, p95>250 ms `sor`).
  Status-Frame `slo_breach: { topic, percentile, value_ms }` im
  Stream — Robot reagiert intern (Risk-Rule), PSM zeigt Indicator.

---

## 5. Test-Strategie

### 5.1 Unit-Tests (pytest, vorhanden)

- `tests/unit/test_topic_adapter_smd.py` — Frame-Parser gegen K1-/K4-
  Recordings; mixed-type-Decoding; Delta-zu-Snapshot-Merge; `tic`-
  und `smd`-Dedup.
- `tests/unit/test_topic_adapter_sor.py` — Order-Lifecycle-Frames aus
  K4 (`PendingSubmit → PreSubmitted → Submitted → Cancelled`),
  `timeInForce`-Quirk-Normalisierung, Bootstrap-Glue.
- `tests/unit/test_subscription_registry.py` — Replay-Verhalten;
  Add/Remove/Refcount; Auth-Loss-Pause.
- `tests/unit/test_ws_push_source.py` — Quelle gegen Mock-Adapter;
  Backpressure-Drop; SLO-Logger.

### 5.2 Integration-Tests

- `tests/integration/test_quotes_stream_ws_source.py` — Ende-zu-Ende
  von WS-Replay (`tests/cp_mock/ws_replay.py`) bis SSE-Egress.
  Verwendet `tests/fixtures/recorded/ws/topic-explorer-2026-04-30/`-
  Mitschnitte; assertet Frame-Anzahl und semantische Felder.
- `tests/integration/test_orders_stream_bootstrap.py` — Bootstrap-
  Pfad (REST-Mock liefert initialen Snapshot, WS-Replay liefert
  Live-Updates), Reihenfolge garantiert.
- `tests/integration/test_reconnect_replay.py` — simulierter
  WS-Disconnect, prüft `SubscriptionRegistry.replay()` und
  `subscription_replay_in_progress`-Status-Frame.

### 5.3 Live-Tests (manuell, ad hoc)

Live gegen U25235077 nur in `scripts/`-Tools, nicht im pytest-Lauf:

- `scripts/ws_topic_explorer.py` (Bestand) erweitern um `sor`-
  Bootstrap-Vergleich (REST vs. WS-Frame).
- `scripts/ws_status_check.py` (neu) — pollt `/v1/status` und
  vergleicht gegen das Wire-Protokoll-Verhalten.

### 5.4 Paper-Suite (AP-08)

L1-`paper_readonly` ist read-only und betrifft den WS-Adapter nur
indirekt (Quotes-Endpoint-Konsistenz). L2/L3-Aggressivität bleibt
einer separaten Karte vorbehalten.

---

## 6. Migration und Backwards-Compat

### 6.1 Phase-Plan

| Phase | Ziel | Garantien für Consumer |
|-------|------|------------------------|
| Heute | REST-Polling-`smd` + REST-`sor` | Frames identisch, keine Latenz-Garantie unter 1 s |
| Phase A (AP-05 erste Iteration) | `smd`-WS-Push aktiv, `sor` weiterhin REST-Bootstrap-only | `/v1/quotes/stream`-Vertrag identisch, Latenz sinkt; Header `X-Stream-Source: ws` informativ im Frame |
| Phase B (AP-05 zweite Iteration) | `sor`-WS-Push aktiv, neuer Endpoint `/v1/orders/stream` | Endpoint additiv; bestehende Order-REST bleibt |
| Phase C (optional) | Per-Subscription-Failure-Mode produktiv | Header `X-Stream-Failure-Mode` opt-in; Default `fail-loud` |

### 6.2 Rollback-Pfad

- **Per-Endpoint-ENV-Schalter** (`BG_QUOTES_SOURCE=ws|polling`,
  `BG_ORDERS_SOURCE=ws|rest`) erlaubt schnellen Rückfall auf den
  Polling-Pfad ohne Code-Revert. Schalter steht in `.env.live` und
  `.env.paper` separat.
- Polling-Quelle bleibt im Code aktiv, bis WS-Source mindestens einen
  ganzen Trading-Tag im Live-Konto unterbrechungsfrei läuft.

### 6.3 Schema-Migration

- v1-Frame-Schema bleibt; nur Quelle wechselt.
- `schema_version=1` ist der Marker für „heutiges Vertragsfeld-Set".
  Nächstes ADR bei jedem zusätzlichen Pflichtfeld (Major-Bump) oder
  semantischen Re-Mapping.
- Adapter hält N-1-Kompatibilität, Consumer-`X-Schema-Version`-Header
  schaltet das alte Schema durch (PSM-Forderung).

---

## 7. AP-05 — vorgeschlagene Karten-Skizzen

> Verbindlich zur Anlage erst nach User-OK in einem getrennten
> `outline`-Lauf. Reihenfolge entspricht Bauphase A → B; Karten 5–8
> sind Phase-B/-C oder querschnittlich.

| # | Titel-Skizze | Soll-Zustand (1 Satz) | Phase |
|---|--------------|-----------------------|-------|
| 1 | TopicAdapter `smd`: Frame-Parser + semantisches Mapping | Mixed-type-Felder dekodiert, Delta-zu-Snapshot gemerged, `tic`-/Doppel-Frame-Dedup, Unit-Tests gegen K4-Recordings grün. | A |
| 2 | SubscriptionRegistry mit Replay nach `connect()` | Soll-State aller aktiven Subscriptions persistiert in-memory, replay nach jedem Reconnect aus Bestandsfindings, Auth-Loss pausiert. | A |
| 3 | WSPushSource für SubscriptionManager + Polling-Quelle als Fallback | `BG_QUOTES_SOURCE=ws` schaltet WS-Push als 1st-class Quelle, Polling bleibt opt-in; `/v1/quotes/stream`-Vertrag unverändert. | A |
| 4 | TopicAdapter `sor` + Bootstrap-Glue mit REST-Initial-Snapshot | `sor`-Frames in semantische Order-Snapshots normalisiert, `timeInForce`-Quirk gemappt, Bootstrap-Frame-Reihenfolge garantiert. | B |
| 5 | `/v1/orders/stream` SSE-Endpoint | Neuer Endpoint analog zu `quotes_stream`, Bootstrap → Live, Scope `read:streams.orders`. | B |
| 6 | Per-Subscription Failure-Mode-Header | `X-Stream-Failure-Mode` mit Werten `fail-loud`/`rest-fallback`/`silent-skip`, Default `fail-loud`, dokumentiert in `docs/api/v1.md`. | C |
| 7 | `/v1/status` Endpoint | Felder `cp_gateway_connected`, `last_frame_age_seconds`, `reconnect_attempt`, `subscriptions_active`; Scope `read:status`. | A oder B |
| 8 | 150-Symbol-`smd`-Stresstest + Backpressure-Karte | Live-Stresstest gegen U25235077, Pacing-Beobachtung, Backpressure-Strategie pro Topic-Adapter, SLO-Bewertung. | C |

---

## 8. Risiken und offene Fragen

### 8.1 Offene Fragen für den User (vor Anlage AP-05 zu klären)

| # | Punkt | Wer | Vorschlag |
|---|-------|-----|-----------|
| 1 | Multi-Tenant-Skalierung (mehrere PSM-User gegen eine broker-gateway-Instanz) | Christian | nicht in AP-05 — Single-Tenant zementieren, Multi-Tenant ist eigenes AP nach PSM-M2 |
| 2 | WS-Egress vom Service zu Consumern (PSM hält das offen, Robot lehnt ab) | Christian | Vorerst SSE-only, WS-Egress als optionale spätere Karte |
| 3 | Per-Konto-Token-Scopes (z.B. `read:streams.quotes:U25235077`) | Christian | nicht in v1; bleibt Coarse-Scope, separate Sicherheits-Karte in AP-10 |
| 4 | `schema_version=2`-Vorabplanung | Christian | nicht jetzt; nächste Major-Änderung triggert ADR |

### 8.2 Technische Risiken

- **K4-`str`-Re-Send-Quirk**: 277 Frames in 60 s für einen Vortags-
  Trade ist nicht erklärt. Da `str` nicht integriert wird, ist das
  Risiko in v1 deferred — bei späterer Integration ist eine eigene
  Recherche-Karte Pflicht.
- **`tic`-4er-Multiplikator**: K3-Client deduppt schon, aber das
  Verhalten ist undokumentiert; bei Major-CP-Gateway-Update neu
  verifizieren.
- **Initial-Snapshot-`sor`-Lücke**: K4 hat keinen Snapshot bei Sub
  ohne offene Orders gesehen — REST-Bootstrap ist Pflicht. Wenn
  IBKR das Verhalten ändert, wird der Bootstrap redundant; nicht
  schlimm, aber im Adapter-Doku zu erwähnen.
- **Latenz-SLO Robot p95<150 ms**: Adapter-intern und CP-Gateway-
  intern gibt es noch ungemessene Anteile. Nach erster Iteration
  empirisch messen; Karte 8 in AP-05 ist genau dieser Punkt.
- **Backpressure**: Slow-Consumer kann den ganzen Stream-Pfad
  ausbremsen. Drop-Oldest in Karte 3, Slow-Consumer-Drop in
  separater Karte (siehe oben).

### 8.3 Konsumenten-Konflikt-Auflösungen (zusammengefasst)

| Punkt | PSM | trading_robot | Adapter-Wahl |
|-------|-----|---------------|--------------|
| Failure-Mode `smd` | REST-Fallback | Fail-Loud | per-Subscription-Header (Default `fail-loud`) |
| Failure-Mode `sor` | Fail-Loud | Fail-Loud + REST-Reconcile | Fail-Loud, Reconcile macht der Robot |
| Latenz-SLO `smd` p95 | < 1500 ms | < 150 ms | Robot-SLO als Adapter-Ziel |
| Skala `smd` | 150 Symbole | 30 Symbole | 150-Cap, mit Stresstest |
| Frame-Schema | semantisch + Voll-Snapshot | semantisch + Voll-Snapshot | identisch übernommen |
| `schema_version` | Pflicht-Frame-Feld + opt. Header | Pflicht-Frame-Feld | beides, wie in 3.3 beschrieben |
| Bei Konflikt | Adapter entscheidet, Robot gewinnt bei Latenz/Skala | dito | dokumentiert, bei strittigen Punkten Robot-Position übernehmen |

---

## 9. Decision-Gate

**Nächster Schritt:** Review durch User. Bei Freigabe Anlage AP-05
mit den in Sektion 7 vorgeschlagenen Karten in einem getrennten
`outline`-Lauf.

Alternative Outcome: Falls der User nach diesem Design entscheidet,
dass kein WS-Adapter gebaut wird (z.B. weil Konsumenten doch ohne
Push auskommen), gilt dieses Dokument als Recherche-Abschluss und
AP-05 wird verworfen. Beide Outcomes sind nach Karten-Notes gültig.

---

## Anhang A — `sor`-Frame-Felder (semantisch)

| Adapter-Feld | IBKR-Quelle (K4) | Bemerkung |
|--------------|------------------|-----------|
| `order_id` | `orderId` | int |
| `client_order_id` | `cOID` (falls vorhanden) | Idempotency-Schlüssel |
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
| `schema_version` | — | constant `1` |

UI-only Felder (`bgColor`, `fgColor`) werden **nicht** durchgereicht.

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
| `availability_code` | 6509 | string (`R`/`D`/...) |
| `high` | 70 | Decimal |
| `low` | 71 | Decimal |
| `server_id` | `server_id`/6119 | string (informational) |
| `conid` | `conid` | int |
| `updated_at` | `_updated` | ISO-8601 |
| `schema_version` | — | constant `1` |

Adapter merged Delta-Frames in den letzten Snapshot-Zustand, sodass
am Egress immer ein Voll-Snapshot pro Frame ankommt.
