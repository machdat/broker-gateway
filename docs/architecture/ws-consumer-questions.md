# WS-Adapter Fragenkatalog an Consumer

> **Was diese Datei ist:** Der `broker-gateway` formuliert hier die
> Fragen, die er beantwortet bekommen muss, bevor in K6 der
> WS-Adapter entworfen wird. Die **Antworten leben nicht hier** —
> sie liegen in der Doku der jeweiligen Consumer-Projekte (PSM,
> trading-robot, ...). broker-gateway ist passiver Nutzer der
> Antworten.
>
> **Was diese Datei NICHT ist:** kein Fragebogen mit Eingabe-Zeilen,
> keine Default-Antworten "aus Adapter-Sicht". Der Adapter darf seine
> Fragen formulieren — aber die Wahl trifft der Consumer.

## 1. Hintergrund

Der `broker-gateway` baut in AP-04 einen produktiven WebSocket-Adapter
auf den IBKR-CP-Gateway-WS-Endpunkt (`/v1/api/ws`). Phase 1 (K1–K4)
ist abgeschlossen und hat das technische Setup verifiziert:

- **K1** Connect-Spike — Auth, Heartbeat, Frame-Format, `tic`-Multiplikator-Quirk.
- **K2** Recording-/Replay-Format — `tests/fixtures/recorded/ws/spike-baseline.jsonl`.
- **K3** `CPWebSocketClient` — Reconnect, Auth-Bestätigung via `sts`-Frame, `tic`-Dedup.
- **K4** Topic-Exploration — Live-Mitschnitte gegen U25235077, sechs Szenarien (smd Single/Multi/Big, sor mit Test-Order, str, Reconnect-Verhalten). Reife-Ranking pro Topic (Anhang A).

**Quelldokument für technische Details:**
[`docs/research/ibkr-cpapi-websockets-findings.md`](../research/ibkr-cpapi-websockets-findings.md).

**Was K5 leistet:** den Fragenkatalog. Fertig sobald die Liste
abgenommen ist.
**Was K6 leistet:** Adapter-Architektur, sobald die Antworten aus den
Consumer-Projekten vorliegen.

---

## 2. Wie der Fragenkatalog zu nutzen ist

### 2.1 Wer antwortet wo

Jedes Consumer-Projekt, das den `broker-gateway` als Push-Quelle
nutzen will, beantwortet diesen Katalog **in seiner eigenen Doku**.
Konvention (aus [`docs/05-api.md`](../05-api.md)):

- **PSM** → `docs/integrations/broker-gateway.md` im PSM-Repo.
- **trading-robot** → `docs/integrations/broker-gateway.md` im
  trading-robot-Repo.
- weitere Consumer analog.

Die Antwort-Datei pro Consumer enthält mindestens:

1. Welche Topics gewünscht sind, mit Symbolen/Konten/Feldern.
2. Latenz-SLO und Failure-Mode-Wahl.
3. Skalen-Erwartung (Symbole, Konten, gleichzeitige Streams).
4. Transport-Wahl (SSE / REST / WebSocket) pro Topic.
5. Priorisierung gegenüber anderen Consumer-Anforderungen.

Format ist dem Consumer-Projekt freigestellt — solange jede Frage
aus diesem Katalog adressiert ist.

### 2.2 Wie broker-gateway die Antworten konsumiert

K6 in `broker-gateway` schaut die Antwort-Dateien der Consumer-Projekte
an und konsolidiert daraus die Adapter-Architektur
(`docs/architecture/ws-adapter-design.md`). Bei Konflikten zwischen
Consumern (z.B. PSM will Snapshot, trading-robot will Delta) wird der
Konflikt explizit dokumentiert und entschieden — broker-gateway rät
nicht.

### 2.3 Wann antworten

Vor Beginn von K6. K6 ohne Consumer-Antworten wäre Raten und ist
deshalb explizit blockiert. Wenn ein Consumer noch nicht antworten
kann, kann K6 mit einer Teilmenge starten (z.B. nur PSM-Antworten,
trading-robot später), aber das muss in K6 dokumentiert sein.

---

## 3. Topic-Auswahl

Pro Topic drei Unterfragen: (a) brauchst du das überhaupt?
(b) wenn ja, für welche Symbole/Konten/Felder? (c) wann subscriben
(immer während Service-Laufzeit, nur Marktöffnung, on-demand pro
Consumer-Aufruf)?

### 3.1 `smd` — Market-Data-Snapshot/Delta (Bid/Ask/Last)

Reife K4: **grün**. Felder klar, Subscribe-Format stabil, 25 Symbole
parallel ohne Pacing-Violation.

- Brauchst du `smd`?
- Wenn ja: welche Symbole, welche Felder, für welche Konten?
- Wann soll subscribed/unsubscribed werden?

### 3.2 `sor` — Smart Order Router / Live Order Updates

Reife K4: **grün**. Order-Lifecycle vollständig beobachtet. Achtung:
kein Initial-Snapshot — `GET /iserver/account/orders` ist
REST-Bootstrap-Pflicht.

- Brauchst du `sor`?
- Welche Konten, welche Felder?
- Dauerhaft oder phasenweise?

### 3.3 `str` — Trades / Executions

Reife K4: **gelb**. 277 Frames in 60 s für einen Vortags-Trade
beobachtet (Re-Send-Quirk). `execution_id`-Dedup im Adapter Pflicht.

- Brauchst du `str` überhaupt, oder reicht REST-Polling auf
  `/iserver/account/{accountId}/trades`?
- Falls ja: welche Konten, welche Felder, welche Frequenz erwartet?
- Tolerierst du Re-Send-Verhalten, oder soll der Adapter ein
  Sampling/Throttling vorschalten?

### 3.4 `spl` — Profit/Loss

Reife K4: nicht getestet.

- Brauchst du `spl`?
- Falls ja: welche Konten, welche Felder, welche Frequenz?

### 3.5 `smh` — Market-Data-History

Reife K4: nicht getestet.

- Brauchst du `smh` als Push, oder reicht REST
  (`/iserver/marketdata/history`)?

### 3.6 `sbd` — Bulletin

Reife K4: nicht getestet, im 75-s-Fenster nicht beobachtet.

- Brauchst du Bulletin-Push?
- Falls ja: nur informativ (Log) oder Operator-Alarmierung?

---

## 4. Latenz-SLO

Push-Daten lohnen sich nur, wenn die Latenz besser ist als REST-Polling.
REST-Baseline gegen lokales CP-Gateway: ~80–150 ms pro Call.

Pro relevantem Topic:

- Welche p50-Latenz ist akzeptabel (Server→Consumer-Egress)?
- Welche p95-Latenz ist akzeptabel?
- Welche p99-Latenz ist akzeptabel?
- Wo soll gemessen werden (CP-Gateway-Receive, Adapter-Emit,
  EventBus-Hop, SSE-Egress)?
- Was ist die Konsequenz bei SLO-Verletzung — nur Logging, Alarm,
  Stream-Abbruch, Fallback auf REST?

---

## 5. Multi-Symbol- und Multi-Konto-Skala

K4 hat 25 Symbole parallel ohne Throttle bestätigt. Höhere Skalen
sind nicht getestet.

- Wie viele Symbole gleichzeitig (top-of-book via `smd`) maximal?
- Wie viele Konten gleichzeitig (live + paper kombiniert)?
- Wie viele gleichzeitige Consumer-Streams pro Konto sind realistisch
  (mehrere SSE-Verbindungen aus demselben Consumer-Prozess)?
- Was soll passieren, wenn ein Consumer mehr Subscriptions anfordert
  als der Adapter sicher händeln kann (Hard-Limit, Soft-Throttle,
  Priority-Queue)?
- Sollen Consumer pro Subscription die Felder einschränken dürfen,
  oder liefert der Adapter immer einen normalisierten Voll-Snapshot?

---

## 6. Failure-Mode-Erwartung

Reconnect-Verhalten ist K4-Reife **rot**: Subscription-State geht
serverseitig verloren. Der Adapter muss alle Subscriptions nach jedem
Reconnect neu absetzen.

### 6.1 Verhalten bei WS-Abbruch (CP-Gateway erreichbar, WS tot)

Drei Optionen, mit Trade-Offs:

- **Fallback REST** — broker-gateway pollt automatisch
  `/iserver/marketdata/snapshot` (oder den jeweiligen REST-Pfad) und
  liefert Updates über denselben Consumer-Stream weiter. Consumer
  merkt nichts.
  *Trade-Off:* Latenz steigt, kein Lifecycle für `sor`/`str`,
  Äquivalenz nicht voll garantiert.
- **Fail-Loud** — Consumer-Stream schliesst mit Error-Frame. Consumer
  muss neu connecten.
  *Trade-Off:* Consumer-Last steigt kurzzeitig, dafür keine Illusion
  von Aktualität.
- **Silent-Skip** — Stream bleibt offen, liefert keine Updates bis
  WS wieder steht.
  *Trade-Off:* Consumer kann nicht unterscheiden, ob "keine
  Bewegung" oder "keine Daten" — gefährlich für Trading-Logik.

Frage pro Topic (smd, sor, str, ggf. weitere): welche Option?

### 6.2 Verhalten bei CP-Gateway-Restart

CP-Gateway selbst startet neu (z.B. nach Container-Restart, 2FA-Re-Auth).
Welches Verhalten erwartet der Consumer in dieser Phase?

### 6.3 Reconnect-Backoff

Welche Reconnect-Strategie ist akzeptabel — sofort, exponentielles
Backoff, fester Takt? Soll der Consumer die Backoff-Strategie
sehen (z.B. über einen Status-Endpunkt) oder ist sie intern?

### 6.4 Subscription-Replay

Soll der Adapter beim Reconnect alle bisherigen Subscriptions
automatisch neu absetzen, oder nur auf Wunsch des Consumers (mit
expliziter Re-Subscribe-Anforderung)?

---

## 7. API-Vertrag für Consumer

Heute liefert `broker-gateway` Streams als **SSE** (`/v1/streams/...`),
mit `Last-Event-ID`-Reconnect-Semantik. WebSocket-Egress an Consumer
ist nicht implementiert.

### 7.1 Transport pro Topic

- Bevorzugst du SSE, REST-Polling oder echtes WebSocket pro Topic?
- Falls SSE: brauchst du `Last-Event-ID`-Reconnect, oder reicht ein
  einfacher Stream ohne Recovery?
- Falls REST-Polling: welcher Takt — 1 s, 5 s, on-demand?
- Falls WebSocket: warum (was kann SSE nicht)?

### 7.2 Frame-Schema am Egress

- Soll der Adapter rohe IBKR-Felder (`6509`, `31`, `84`, ...) oder
  semantische Felder (`availability_code`, `last`, `bid`, ...)
  liefern?
- Bei `smd`: soll jeder Frame ein voller Snapshot sein (Adapter hält
  den Last-State pro Subscription) oder ein Delta (nur geänderte
  Felder)?
- Bei `sor`: Voll-Snapshot pro Status-Wechsel oder Delta-Frames mit
  separaten Snapshot-Touchpoints?
- Soll der Adapter eine stabile Versionsnummer pro Frame-Schema
  liefern, damit Consumer beim Schema-Wechsel überlauf-frei
  migrieren können?

### 7.3 Authentifizierung

- Bleibt Bearer-Token wie in [`docs/04-security.md`](../04-security.md)
  beschrieben?
- Welche Scopes sollen Push-Streams gegenüber den heutigen REST-Scopes
  fordern (z.B. `read:streams.quotes`, `read:streams.orders`,
  `read:streams.trades`)?
- Brauchst du am Stream einen Liveness-Heartbeat (z.B. SSE-Comment
  alle 15 s) für Proxy-Stabilität?

---

## 8. Priorisierung

K4 empfiehlt: AP-05 erste Iteration nur `smd`, weil das die einzige
**grün**-Topic mit klarem Mehrwert ist.

- In welcher Reihenfolge soll der Adapter die Topics integrieren?
- Welche Topics können aus AP-05/AP-06 ausgeschlossen werden?
- Gibt es ein hartes Lieferdatum, oder kann AP-05 über mehrere
  Sprints gehen?
- Falls mehrere Consumer parallel Anforderungen haben: welche
  Reihenfolge ist bei Konflikten dominant?

---

## Anhang A — K4-Reife-Ranking

Aus
[`docs/research/ibkr-cpapi-websockets-findings.md`](../research/ibkr-cpapi-websockets-findings.md),
Sektion *Topic — Reife*:

| Topic | Reife | Begründung |
|-------|-------|-------------|
| `smd` | **grün** | Felder klar, Subscribe-Format stabil, kein Pacing bei 25 Symbolen. Mixed-type-Werte (string vs. float) handhabbar. |
| `sor` | **grün** | Order-Lifecycle in 6 Frames vollständig. Confirmations-Flow (3 Reply-Schritte) und Delta-vs-Snapshot-Mix bekannt. Initial-Snapshot fehlt — REST-Bootstrap nötig. |
| `str` | **gelb** | Initial-Burst gut, aber 277 Frames in 60 s für einen Vortags-Trade suspekt. `execution_id`-Dedup Pflicht. Wiederhol-Mitschnitt mit frischem Trade nötig. |
| Reconnect | **rot** | Subscription-State serverseitig verloren — Adapter muss eigenen Subscription-Cache halten und nach jedem `connect()` replayen. |

## Anhang B — Bezug zu K6 / AP-05

K6 (Card-ID `b2c1d27e-7e94-4b2f-a8e2-efce39f7b8bc`, AP-04, Prio 9) ist
der Folge-Schritt. K6 nimmt die Antworten aus den Consumer-Projekten
(siehe Sektion 2.1) und entwirft die Adapter-Architektur
(Subscription-Manager, Topic-Adapter, EventBus-Producer).

AP-05 baut den produktiven Adapter auf Basis von K6.

K6 ist explizit blockiert, solange noch kein Consumer den Katalog
beantwortet hat.

## Anhang C — Adapter-Standpunkt (optional, Diskussions-Hilfe)

> **Klar getrennt von den Fragen oben.** Dieser Anhang ist eine
> kompakte Adapter-Bauchgefühl-Sicht, **kein Default und keine
> Empfehlung**. Consumer können diesen Anhang ignorieren oder als
> Sparring-Vorlage nutzen, um schneller zu einer eigenen Position zu
> kommen. Bei Widerspruch zwischen Consumer-Antwort und diesem
> Anhang gewinnt die Consumer-Antwort.

- **Topic-Reihenfolge für den Adapter selbst:** `smd` zuerst (grün,
  hoher Mehrwert), dann `sor` (grün, kompakt), zuletzt `str` (gelb,
  vorher Mitschnitt-Wiederholung).
- **Frame-Schema am Egress:** semantische Felder bevorzugt, weil rohe
  IBKR-Field-IDs sonst in jeden Consumer leaken und brechen, sobald
  IBKR umbenennt.
- **Voll-Snapshot vs. Delta:** technisch ist Voll-Snapshot für
  `Last-Event-ID`-Reconnect simpler, weil der Adapter den Last-State
  bereits halten muss.
- **Failure-Mode:** `Silent-Skip` ist die gefährlichste Wahl, weil
  Consumer-Code "keine Updates" leicht mit "keine Bewegung am Markt"
  verwechselt.
- **Reconnect:** Adapter hält Subscription-Cache und replayt
  automatisch — sonst muss jeder Consumer den Cache nachbauen.

Diese Punkte sind keine Spec, nur ein Sparringpartner.
