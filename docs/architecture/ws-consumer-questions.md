# WS-Adapter Consumer-Fragebogen

> **Status:** offen — Antworten werden offline mit den Consumer-Ownern
> (PSM, trading-robot, jeweils Christian) eingeholt und fliessen in
> AP-04 K6 (WS-Adapter-Architektur) und AP-05 (produktiver WS-Adapter)
> ein.
>
> **Adressat:** Consumer-Owner. Der Fragebogen klingt direkt — bewusst.
> Bauchgefuehl-Defaults stehen unter jeder Frage; jede Frage hat eine
> Eingabe-Zeile fuer die finale Antwort. Kein Default ist verbindlich,
> bis er bestaetigt ist.

## 1. Hintergrund

Der `broker-gateway` baut in AP-04 einen produktiven WebSocket-Adapter
auf den IBKR-CP-Gateway-WS-Endpunkt (`/v1/api/ws`). Phase 1 (K1–K4)
ist abgeschlossen und hat das technische Setup verifiziert:

- **K1** Connect-Spike — Auth, Heartbeat, Frame-Format, `tic`-Multiplikator-Quirk.
- **K2** Recording-/Replay-Format — `tests/fixtures/recorded/ws/spike-baseline.jsonl`, JSONL-Frames mit `{ts, dir, topic, raw, parsed}`.
- **K3** `CPWebSocketClient` — Reconnect, Auth-Bestaetigung via `sts`-Frame, `tic`-Dedup.
- **K4** Topic-Exploration — Live-Mitschnitte gegen U25235077, sechs Szenarien (smd Single/Multi/Big, sor mit Test-Order, str, Reconnect-Verhalten). Reife-Ranking pro Topic.

**Quelldokument fuer technische Details:**
[`docs/research/ibkr-cpapi-websockets-findings.md`](../research/ibkr-cpapi-websockets-findings.md).
Insbesondere die K4-Ranking-Tabelle (Sektion *Topic — Reife*) ist die
Basis fuer die Topic-Auswahl-Fragen unten.

**Was diese Karte (K5) leistet:** sie sammelt die offenen
Consumer-Anforderungen, die wir nicht raten wollen, *bevor* K6 die
Adapter-Architektur entwirft. Antworten werden in K6 als
`docs/architecture/ws-adapter-design.md` umgesetzt.

**Was diese Karte NICHT leistet:** sie holt die Antworten nicht ein.
Das macht der User offline und traegt die Antworten in dieses
Dokument als Folge-Commit nach.

---

## 2. Topic-Auswahl

Pro Topic drei Unterfragen: (a) brauchst du das ueberhaupt? (b) Wenn
ja, fuer welche Symbole/Konten/Felder? (c) Wann (immer waehrend
Service-Laufzeit, nur Marktoeffnung, on-demand pro Consumer-Aufruf)?

### 2.1 `smd` — Market-Data-Snapshot/Delta (Bid/Ask/Last)

**Reife K4: gruen.** Felder klar, Subscribe-Format stabil, 25 Symbole
parallel ohne Pacing-Violation, ~3 Frames/s aggregiert.

- **Brauchst du `smd`?**
  - Default: **Ja, im PSM** (Live-Quotes fuer Watchlist-Symbole und
    Position-Marking). Fuer trading-robot vermutlich auch, aber nur
    waehrend aktiver Trading-Phasen.
  - Antwort: ____________________________________________________

- **Fuer welche Symbole/Konten/Felder?**
  - Default: PSM-Watchlist (~10–20 Symbole, dynamisch), Felder 31/84/86
    (Last, Bid, Ask) plus 6509 (Availability-Code DPB/RPB) als Pflicht
    fuer Datenqualitaet. trading-robot: nur die Symbole, die er
    aktuell handelt (1–5).
  - Antwort: ____________________________________________________

- **Wann subscriben?**
  - Default: `smd` nur waehrend Markt offen (RTH, ggf. ETH wenn
    Watchlist-Symbol Pre/After-Market handelt). Subscribe on-demand
    pro Consumer-Anfrage, nicht eager.
  - Antwort: ____________________________________________________

### 2.2 `sor` — Smart Order Router / Live Order Updates

**Reife K4: gruen.** Voller Order-Lifecycle in <50 ms ueber 6 Frames
beobachtet (PendingSubmit → PreSubmitted → Submitted → PendingCancel →
Cancelled). Achtung: kein Initial-Snapshot — `GET /iserver/account/orders`
muss vor `sor`-Subscribe gepollt werden.

- **Brauchst du `sor`?**
  - Default: **Ja, in beiden Consumern.** PSM braucht es fuer den
    Portfolio-Status (welche Orders sind offen?), trading-robot braucht
    Echtzeit-Bestaetigung der eigenen Orders.
  - Antwort: ____________________________________________________

- **Fuer welche Konten/Felder?**
  - Default: alle Konten, die der Consumer hat (aktuell nur U25235077).
    Felder: orderId, status, filled/remaining, avgPrice, side, ticker,
    tif, lastExecutionTime. UI-Hint-Felder (`bgColor`, `fgColor`)
    droppen.
  - Antwort: ____________________________________________________

- **Wann subscriben?**
  - Default: dauerhaft, solange der Consumer aktiv ist. Order-Events
    sind selten genug (≤1 Frame/min idle), da lohnt sich kein
    on-demand.
  - Antwort: ____________________________________________________

### 2.3 `str` — Trades / Executions

**Reife K4: gelb.** Initial-Burst liefert Historie der letzten 6 Tage,
aber 277 Frames in 60 s fuer einen einzelnen Vortags-Trade ist
suspekt (vermutlich Re-Send-Mechanismus). `execution_id`-Dedup im
Adapter Pflicht.

- **Brauchst du `str`?**
  - Default: **Ja, im PSM** (Trade-History fuer Performance-Berechnung
    und Reconciliation). trading-robot vermutlich auch, aber nur fuer
    eigene Order-IDs.
  - Antwort: ____________________________________________________

- **Fuer welche Konten/Felder?**
  - Default: alle Konten. Felder: execution_id, conid, side, size,
    price, commission, trade_time, trade_time_r, account, exchange,
    sec_type, order_id. `order_description` (textuell) optional.
  - Antwort: ____________________________________________________

- **Wann subscriben?**
  - Default: dauerhaft, mit Re-Send-Dedup. Falls der 277-Frame-Quirk
    bleibt: Backpressure-Mechanismus im Adapter (z.B. max 1
    Forward-Event pro `execution_id` und 5 s).
  - Antwort: ____________________________________________________

- **Unangenehme Frage:** Reicht alternativ ein REST-Polling auf
  `/iserver/account/{accountId}/trades` alle 30 s?
  - Default: REST-Polling waere die ehrlichere Loesung, solange `str`
    nicht stabilisiert ist. WS-Push lohnt sich nur, wenn die Latenz
    relevant ist (also <500 ms).
  - Antwort: ____________________________________________________

### 2.4 `spl` — Profit/Loss

**Reife K4: nicht getestet.**

- **Brauchst du `spl`?**
  - Default: **Wahrscheinlich nicht.** PSM berechnet PnL selbst aus
    Positionen + Marktpreisen. trading-robot interessiert nur die
    eigene Position-PnL, die er aus `sor` + `smd` ableiten kann.
  - Antwort: ____________________________________________________

- **Falls ja, Konten/Felder/Frequenz:** _________________________________

### 2.5 `smh` — Market-Data-History

**Reife K4: nicht getestet.**

- **Brauchst du `smh`?**
  - Default: **Nein.** Historische Bars holt PSM via REST
    (`/iserver/marketdata/history`) — eine WS-Subscription bringt
    keinen Mehrwert fuer Bars, die ohnehin gepuffert werden.
  - Antwort: ____________________________________________________

### 2.6 `sbd` — Bulletin

**Reife K4: nicht getestet, im 75-s-Fenster nicht beobachtet.**

- **Brauchst du `sbd`?**
  - Default: **Nein bei Erstintegration**, aber niedrige Hemmschwelle
    fuer Nachzug (Bulletin enthaelt IBKR-Service-Hinweise, koennte fuer
    Operator-Alarmierung relevant werden).
  - Antwort: ____________________________________________________

---

## 3. Latenz-SLO

Push-Daten lohnen sich nur, wenn die Latenz besser ist als REST-Polling.
REST-Baseline: ~80–150 ms pro Call gegen lokales CP-Gateway.

### 3.1 `smd` Latenz-Erwartung

- **p50 / p95 / p99 Server→Consumer (broker-gateway zaehlt mit) — was ist akzeptabel?**
  - Default: p50 < 50 ms, p95 < 200 ms, p99 < 500 ms. Alles dahinter
    ist schlechter als ein einfacher REST-Poll alle 1 s.
  - Antwort: p50 ____ ms, p95 ____ ms, p99 ____ ms

- **Was ist die Konsequenz, wenn die SLO gerissen wird?**
  - Default: **Soft-SLO** — Latenz-Verletzung wird gelogged
    (`event=stream.latency.violation`), Service laeuft weiter. Erst
    wenn p95 dauerhaft >500 ms ueber 5 min, Alarm an Operator.
  - Antwort: ____________________________________________________

### 3.2 `sor` Latenz-Erwartung

- Default: p50 < 100 ms, p95 < 500 ms (Order-Status-Update muss
  bevor der Trader manuell reagiert ankommen — das definiert die
  Obergrenze).
- Antwort: p50 ____ ms, p95 ____ ms, p99 ____ ms

### 3.3 `str` Latenz-Erwartung

- Default: p95 < 2 s reicht (Trade-Reconciliation passiert
  asynchron).
- Antwort: p95 ____ ms

### 3.4 Globaler Latenz-Skopus

- **Wie soll die Latenz gemessen werden?**
  - Default: WS-Frame-`ts` (CP-Gateway-Empfang) bis SSE-Emit am
    `broker-gateway`-Egress. Zwischenmessungen: WS-Receive,
    Adapter-Emit, EventBus-Hop, SSE-Flush.
  - Antwort: ____________________________________________________

---

## 4. Multi-Symbol- und Multi-Konto-Skala

K4 hat 25 Symbole parallel ohne Throttle bestaetigt. Hoehere Skalen
sind nicht getestet.

### 4.1 Maximale Symbol-Anzahl gleichzeitig (top-of-book via `smd`)

- Default: **bis 50 Symbole** in PSM (Watchlist + Positionen +
  Marktindex-Tracking). trading-robot deutlich weniger (~5).
- Antwort: ____ Symbole maximal

### 4.2 Konten-Anzahl

- Default: **1 Konto** (U25235077). Mittelfristig moeglicherweise ein
  Paper-Account dazu, dann 2.
- Antwort: ____ Konten

### 4.3 Verhalten bei Limit-Ueberschreitung

- **Was soll passieren, wenn der Consumer mehr Subscriptions
  anfordert als der Adapter sicher haendeln kann?**
  - Default: **Fail-Loud** — neue Subscribe-Anfrage gibt 429 mit
    `error: subscription.limit.exceeded` zurueck. Kein Silent-Drop
    bestehender Subscriptions.
  - Antwort: ____________________________________________________

### 4.4 Felder-Verschwendung vs. Bandbreite

- **Sollen Consumer pro Subscription die Felder explizit
  einschraenken duerfen, oder liefert der Adapter immer einen
  vollen normalisierten Snapshot?**
  - Default: **Normalisierter Snapshot ist Standard.** Felder-Filter
    nur, wenn ein Consumer messbar ueber das EventBus-Volumen klagt
    — bislang kein Engpass.
  - Antwort: ____________________________________________________

---

## 5. Failure-Mode-Erwartung

Reconnect-Verhalten ist K4-Reife **rot**: Subscription-State geht
serverseitig verloren. Der Adapter muss alle Subscriptions nach
jedem Reconnect neu absetzen.

### 5.1 Was passiert, wenn die WS-Verbindung 30 s ausfaellt?

- **Option A — Fallback REST:** broker-gateway pollt automatisch
  `/iserver/marketdata/snapshot` und liefert Updates ueber denselben
  SSE-Stream weiter. Consumer merkt nichts.
- **Option B — Fail-Loud:** SSE schliesst mit
  `event: error\ndata: {"code":"stream.lost"}\n\n`, Consumer muss
  neu connecten.
- **Option C — Silent-Skip:** SSE bleibt offen, aber liefert keine
  Updates bis WS wieder steht. Consumer sieht "Stille".
- Default: **B fuer alle drei Topics** (`smd`, `sor`, `str`). Begruendung:
  Silent-Skip ist eine Falle (Consumer haelt veraltete Daten fuer
  aktuell), Fallback-REST ist fuer `sor`/`str` nicht aequivalent
  (kein Order-Lifecycle, kein Execution-Detail).
- Antwort smd: ____________________________________________________
- Antwort sor: ____________________________________________________
- Antwort str: ____________________________________________________

### 5.2 Was passiert, wenn das CP-Gateway selbst neu startet?

- Default: **B (Fail-Loud)** — Consumer-Stream schliesst,
  broker-gateway ruft `/iserver/auth/status` und reauthenticate-Pfad
  durch. Erst nach erfolgreicher Re-Auth akzeptiert er neue
  SSE-Verbindungen.
- Antwort: ____________________________________________________

### 5.3 Reconnect-Backoff

- Default: 1 s → 2 s → 5 s → 15 s → 60 s (Cap). Subscription-Replay
  beim 1. Reconnect-Versuch noch nicht (warten auf `sts`-OK), beim
  2. Versuch eager.
- Antwort: ____________________________________________________

---

## 6. API-Vertrag fuer Consumer

Heute liefert `broker-gateway` Streams als **SSE** (`/v1/streams/...`),
mit `Last-Event-ID`-Reconnect-Semantik. WebSocket-Egress an Consumer
ist nicht implementiert.

### 6.1 Bevorzugter Transport pro Topic

- **`smd` — SSE / REST-Polling / WebSocket?**
  - Default: **SSE** (passt zum bestehenden Quote-Stream-Muster
    aus AP-03). REST-Snapshot-Endpunkt zusaetzlich fuer
    Bootstrap-Reads.
  - Antwort: ____________________________________________________

- **`sor` — SSE / REST / WebSocket?**
  - Default: **SSE** + REST-Snapshot (`/v1/orders/active`). Order-
    Mutationen (POST/DELETE) bleiben REST.
  - Antwort: ____________________________________________________

- **`str` — SSE / REST / WebSocket?**
  - Default: **SSE** + REST-Range-Endpunkt
    (`/v1/trades?from=...&to=...`) fuer historische Reconciliation.
  - Antwort: ____________________________________________________

### 6.2 Frame-Schema am SSE-Egress

- **Soll der Adapter rohe IBKR-Felder (`6509`, `31`, `84`, ...) oder
  semantische Felder (`availability_code`, `last`, `bid`, ...)
  liefern?**
  - Default: **semantisch.** Die IBKR-Field-IDs leaken sonst in jeden
    Consumer und brechen, sobald IBKR umbenennt. Mapping-Tabelle pro
    Topic in `docs/api/v1.md` dokumentieren.
  - Antwort: ____________________________________________________

- **Soll bei `smd` jeder Frame ein voller Snapshot oder ein Delta
  sein?**
  - Default: **voller normalisierter Snapshot** am Egress, auch wenn
    IBKR Deltas pusht. Adapter haelt den Last-State pro
    Subscription. Begruendung: Consumer brauchen kein Delta-Decoding,
    Last-Event-ID-Reconnect funktioniert dann sauber.
  - Antwort: ____________________________________________________

### 6.3 Authentifizierung am Egress

- Default: weiterhin Bearer-Token wie in `docs/04-security.md`
  beschrieben — `read:streams.quotes`, `read:streams.orders`,
  `read:streams.trades` als Scope-Erweiterung. Keine Cookie-Auth, kein
  IP-Whitelist.
- Antwort: ____________________________________________________

---

## 7. Priorisierung (Topic-Integration-Reihenfolge)

K4 empfiehlt: AP-05 erste Iteration nur `smd`. `sor`/`str` warten
auf K5-Antworten.

### 7.1 Reihenfolge der Topic-Integration

- Default: **`smd` zuerst** (klar, gruen, hoher Mehrwert), dann
  `sor` (gruen, wichtig fuer trading-robot), zuletzt `str` (gelb,
  vorher Mitschnitt-Wiederholung mit aktivem Konto).
- Antwort: 1. ______ 2. ______ 3. ______ (4. ______ 5. ______ 6. ______)

### 7.2 Kann ein Topic ausgeschlossen werden?

- Default: `spl` und `smh` aus AP-05/AP-06 raus.  `sbd` als
  Operator-Hilfsmittel parken.
- Antwort: ____________________________________________________

### 7.3 Hartes Lieferdatum?

- Default: keins — broker-gateway laeuft heute mit REST-Quotes
  fuer PSM stabil. AP-05 kann ueber 2–3 Sprints gehen.
- Antwort: ____________________________________________________

---

## Anhang A — K4-Reife-Ranking (Quelle)

Aus
[`docs/research/ibkr-cpapi-websockets-findings.md`](../research/ibkr-cpapi-websockets-findings.md),
Sektion *Topic — Reife*:

| Topic | Reife | Begruendung |
|-------|-------|-------------|
| `smd` | **gruen** | Felder klar, Subscribe-Format stabil, kein Pacing bei 25 Symbolen. Mixed-type-Werte (string vs. float) handhabbar. |
| `sor` | **gruen** | Order-Lifecycle in 6 Frames vollstaendig. Confirmations-Flow (3 Reply-Schritte) und Delta-vs-Snapshot-Mix bekannt. Initial-Snapshot fehlt — REST-Bootstrap noetig. |
| `str` | **gelb** | Initial-Burst gut, aber 277 Frames in 60 s fuer einen Vortags-Trade suspekt. `execution_id`-Dedup Pflicht. Wiederhol-Mitschnitt mit frischem Trade noetig. |
| Reconnect | **rot** | Subscription-State serverseitig verloren — Adapter muss eigenen Subscription-Cache halten und nach jedem `connect()` replayen. |

## Anhang B — Konkrete Folge-Fragen aus K4-Findings

Direkt aus K4 (`docs/research/ibkr-cpapi-websockets-findings.md`,
Sektion *Konsequenzen fuer K5 / K6 / AP-05*):

1. **`smd` Delta vs. Snapshot:** Brauchen PSM/trading-robot
   `smd`-Delta-Updates oder einen normalisierten Quote-Snapshot pro
   Tick? — siehe Abschnitt 6.2 oben.
2. **`sor` Delta vs. Snapshot:** Soll `sor` Order-Updates als Deltas
   (kompakt) oder als materialisierte Snapshots (immer voll)
   ausgeliefert werden? — siehe Abschnitt 6.2 oben.
3. **`str` Frequenz:** Welche `str`-Frequenz ist realistisch fuer das
   Konto — braucht es Sampling/Throttling? — siehe Abschnitt 2.3
   "Unangenehme Frage" oben.

## Anhang C — Bezug zu K6 / AP-05

K6 (Card-ID `b2c1d27e-7e94-4b2f-a8e2-efce39f7b8bc`, AP-04, Prio 9)
ist der Folge-Schritt: er nimmt die hier eingetragenen Antworten und
entwirft die Adapter-Architektur (Subscription-Manager,
Topic-Adapter, EventBus-Producer). AP-05 baut den produktiven Adapter
auf Basis von K6.

Solange dieser Fragebogen offen ist, sollte K6 NICHT gestartet
werden — sonst wird im Architektur-Design geraten, was Consumer
brauchen.
