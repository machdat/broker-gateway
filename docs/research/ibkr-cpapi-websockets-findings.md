# IBKR Client Portal API - WS-Connect-Spike Findings (2026-04-29)

Praktische Beobachtungen aus dem Throwaway-Spike (KanPrompt K1,
Skript ``scripts/ws_spike.py``). Begleitdoku zur Doku-Snapshot
``ibkr-cpapi-websockets.md`` - hier landet, was real anders ist.

Mitschnitt: ``tests/fixtures/recorded/ws/spike-2026-04-29.jsonl``
(75 s gegen lokales CP-Gateway via SSH-Tunnel zu cma-pi-1, Konto
U25235077, Build 10.45.1a vom 2026-04-22).

## Setup-Realitaet

| Aspekt | Doku-Snapshot | Real beobachtet |
|--------|---------------|-----------------|
| URL-Schema | ``https://localhost:5000`` / ``wss://localhost:5000`` | ``http://localhost:5000`` / ``ws://localhost:5000`` |
| TLS-Layer | TLS bis Container | Tunnel ist die Vertraulichkeitsschicht; Container exposed Plain-HTTP |
| Cookie-Auth | ``Set-Cookie`` aus Browser-Login | ``/tickle``-Response setzt ``x-sess-uuid`` via ``Set-Cookie`` (HttpOnly, ``secure=true``, path=``/v1/api``) |
| Auth-Frame | ``{"session": "<sessionId>"}`` als erster WS-Send | Genau so akzeptiert. Server antwortet ohne Echo - Auth-Bestaetigung kommt indirekt ueber den naechsten ``sts``-Frame |

Der WS-Connect funktioniert mit ``ws://`` ohne TLS-Kontext und mit dem
einfachen ``Cookie``-Header aus der ``/tickle``-Response. Insecure-SSL-
Logik im Skript ist Defensiv-Code fuer den Fall, dass ``wss://``
forciert wird.

## Frame-Format - Abweichung zur Doku

Doku beschreibt das Format als ``TOPIC+{ARGUMENTS}`` (3-Buchstaben-Topic,
``+``-Trenner, JSON-Body). Praktisch gilt:

- **Vom Server gepusht** sind alle Frames **reines JSON** mit
  ``{"topic": "<name>", ...}`` - kein ``+``-Trenner. Beispiel:
  ``{"topic":"system","hb":1777415611502}``.
- **An den Server geschickt** wird das ``TOPIC+{...}``-Format - aber
  Sonderfaelle bestaetigt: ``tic`` ohne ``+`` und ohne JSON-Body wird
  akzeptiert.

Das ``TOPIC+{...}``-Format ist also **subscribe-spezifisch** (Client →
Server), die Server-Pushes nutzen reines JSON. Der Frame-Parser im
Spike toleriert beides; produktiver WS-Adapter wird wahrscheinlich nur
JSON parsen muessen.

## Unsolicited Topics direkt nach Connect

In dieser Reihenfolge innerhalb < 250 ms nach Auth-Send:

1. ``system`` mit ``{"success":"<username>","isFT":false,"isPaper":false}``
   - Doku spricht hier von ``success: <username>`` als Heartbeat-
     Variante; real ist das der **Login-Erfolgs-Frame** (kein ``hb``-
     Feld, dafuer die User-Identitaet + Account-Flags).
2. ``act`` mit der vollen Account-Definition: ``accounts``,
   ``acctProps`` (``supportsCashQty``, ``supportsFractions``, ...),
   ``aliases``, ``allowFeatures`` (40+ Flags incl. ``allowedAssetTypes``-
   String), ``chartPeriods`` pro AssetType, ``selectedAccount``,
   ``serverInfo`` (``serverName``, ``serverVersion``), ``sessionId``
   (anderer Wert als der ``/tickle``-sessionId - Server-interne ID),
   ``isFT``, ``isPaper``. Sehr nahe an ``/iserver/accounts``-REST.
3. ``sts`` mit ``{"connected":true,"authenticated":true,"established":
   true,"competing":false,"message":"","fail":"",...}`` plus
   ``serverName``/``serverVersion``/``username``.

**Nicht beobachtet** im 75-s-Idle-Fenster: ``ntf`` (Trade-Notification),
``blt`` (Bulletin) - braucht Trade-Aktivitaet bzw. IBKR-Bulletin-
Trigger, fuer den Spike erwartungsgemaess leer.

## Heartbeat-Frequenz

``system+hb`` kommt **alle 10 s ± 30 ms** mit ``{"hb": <unix-ms>}``.
Im Mitschnitt: 6 Heartbeats in 70 s (10 s, 10 s, 10 s, 10 s, 10 s, 10 s)
- Decken Doku-Aussage "Heartbeat alle 10s" exakt.

## ``tic``-Verhalten - groesste Ueberraschung

Doku beschreibt ``tic`` als reinen Client-Ping ("mind. 1/min", "ersetzt
NICHT ``/tickle`` REST"). Real:

- **Pro ``tic``-Send antwortet der Server mit 4 identischen ``tic``-
  Frames** (vier mal hintereinander, alle innerhalb < 1 ms,
  identisches ``lastAccessed``-Timestamp). Beispiel:

  ```
  out tic                                            (00:33:59.564)
  in  tic { "alive":true, "id":"<sess>", ... }       (00:33:59.656)
  in  tic { "alive":true, "id":"<sess>", ... }       (00:33:59.656)
  in  tic { "alive":true, "id":"<sess>", ... }       (00:33:59.657)
  in  tic { "alive":true, "id":"<sess>", ... }       (00:33:59.657)
  ```

  Wiederholt sich beim zweiten Ping eins zu eins. Vermutung: der
  Container hat 4 ``tic``-Subscriber-Slots (interne Replikation) oder
  bestaetigt ``tic`` an alle aktiven Auth-Pfade gleichzeitig. Das ist
  nirgends in der Doku erwaehnt.

- ``tic``-Antwort enthaelt ``{"topic":"tic","alive":true,"id":"<sessio
  nId>","lastAccessed":<unix-ms>}`` - ``id`` ist die ``/tickle``-
  sessionId (nicht die ``act``-sessionId).

**Konsequenz fuer K3 (WS-Client):** Der WS-Adapter muss den
4er-Multiplikator beim ``tic``-Receive einplanen, sonst zaehlt jede
Zustandsmaschine 4 Pings statt 1. Entweder den Multiplikator
dokumentieren oder per ``lastAccessed``-Dedup unterdruecken.

## Idle-Verhalten ohne Subscribe

In den 75 s wurde nichts subscribed. Server schickt:

- 6× ``system+hb`` (10s-Takt)
- 1× initialer ``system+success`` + ``act`` + ``sts``
- 4× ``tic``-Antwort pro Client-``tic`` (2 Pings im Fenster -> 8 Frames)

Das heisst: **eine ungenutzte WS-Verbindung zieht ~6 push-Frames/min**
(nur Heartbeats). Plus ein moderates Burst-Volumen beim Connect
(~3 Frames). Subscriptions kommen on top.

## Konsequenzen fuer K2 (Recording-Schema) und K3 (WS-Client)

1. **Recording-Schema (K2)**: pro Frame ``{ts, dir (in/out/meta),
   topic, raw, parsed}`` reicht aus. ``dir=meta`` fuer Connect/
   Disconnect-Marker. ``topic`` darf ``"?"`` sein, wenn der Parser
   nichts ableiten kann (Robustheit gegenueber Format-Drift). JSONL
   ist gut handhabbar.
2. **WS-Client (K3)**:
   - Default-Schema im Code ``ws://`` mit ENV-Override fuer ``wss://``.
   - Cookie-Reuse aus REST-Session reicht; kein Browser-Login-
     Hijacking noetig.
   - Auth-Erfolg pruefen: ``sts``-Frame mit ``args.authenticated=true``
     waehrend der ersten 1-2 s nach Connect. ``system+success`` ist
     ergaenzend, aber kein eindeutiges Auth-Signal.
   - ``system+hb`` als Liveness-Probe nutzen - bleibt ``hb`` >
     ~25 s aus, ist die Verbindung tot.
   - ``tic``-Dedup einbauen (gleiche ``lastAccessed`` in < 50 ms ->
     1 Event), sonst zaehlt der EventBus jede Antwort 4×.
   - ``act``-Frame ist quasi ``/iserver/accounts``-Push - das kann den
     existierenden Init-Pfad in ``cp/lifecycle.py`` ergaenzen.

## Recording-Format (kanonisch fuer Replay)

Festgelegt in K2 (siehe ``tests/cp_mock/ws_replay.py`` +
``tests/fixtures/recorded/ws/spike-baseline.jsonl``). Strikt getrennt
vom REST-Recording-Format - WS hat eine andere Semantik (bidirektional,
frame-basiert, Inter-Frame-Timing relevant), darum ein eigener Pfad
und eine eigene Loader-Klasse.

### Datei-Layout

- Verzeichnis: ``tests/fixtures/recorded/ws/``
- Datei pro Spike-/Recording-Lauf: ``spike-YYYY-MM-DD.jsonl`` plus
  optional eine kanonische Baseline-Fixture ``spike-baseline.jsonl``.
- Eine Zeile = ein Frame (JSON Lines, UTF-8, LF). Keine Wrapper-Hierarchie.

### Frame-Schema

```json
{
  "ts":     "2026-04-28T22:33:29.729+00:00",
  "dir":    "in",
  "topic":  "system",
  "raw":    "{\"topic\":\"system\",\"hb\":1700000000000}",
  "parsed": {"topic": "system", "hb": 1700000000000}
}
```

| Feld | Pflicht | Typ | Bedeutung |
|------|---------|-----|-----------|
| ``ts`` | ja | ISO-8601 mit ms+Timezone | Wallclock-Zeit beim Frame-Empfang/-Send. UTC bevorzugt. |
| ``dir`` | ja | ``"in"`` / ``"out"`` / ``"meta"`` | Server -> Client / Client -> Server / Skript-Marker (z.B. Connect-Event) |
| ``topic`` | ja | String | Aus dem JSON-Body (``parsed.topic``) oder aus dem ``TOPIC+{...}``-Praefix. ``"?"``, wenn der Parser nichts ableiten kann. |
| ``raw`` | ja | String | Wire-Format, unveraendert. Auch leer/``"tic"`` zulaessig. |
| ``parsed`` | nein | object/null | Bereits dekodiertes JSON, falls der Frame parsbar war. Nicht-JSON-Frames lassen ``parsed=null``. |

**Backwards-Compat-Regel:** Zusaetzliche Felder in einer Frame-Zeile
(z.B. spaeter ``session_id``, ``correlation_id``) werden im Loader
in ``WSFrame.extras`` durchgereicht. Aeltere Tests bleiben gruen,
weil sie nur die fuenf Pflichtfelder lesen. Pflichtfelder werden
**nie** entfernt oder umbenannt - solche Aenderungen sind ein
neues Schema unter neuem Verzeichnis.

### Replay-Modi

- ``iter_server_frames(frames)`` - filtert auf ``dir=="in"``. Tests
  fuer den WS-Client (K3) replayen damit, was der Echt-Server gepusht
  hat, ohne dass ein WS-Server-Socket aufgemacht werden muss.
- ``iter_client_frames(frames)`` - filtert auf ``dir=="out"``. Nuetzlich
  fuer Tests, die einen Server-Stub gegen die echten Client-Frames
  laufen lassen (Auth-Format, Subscribe-Format, ``tic``-Takt).

Inter-Frame-Delay ist standardmaessig 0 (synchroner Test-Pfad). Mit
``timing="compressed"`` und ``compression_factor`` kann ein Test
realistische Pausen zwischen Frames einplanen, ohne 75 s zu blockieren.

## Topic-Exploration (K4, 2026-04-30)

Live-Mitschnitte mit ``scripts/ws_topic_explorer.py`` gegen die
U25235077-Session. Output unter
``tests/fixtures/recorded/ws/topic-explorer-2026-04-30/``. Sechs
Szenarien (a-f), alle gegen das interne ``ws://cpgateway:5000/v1/api/ws``
aus dem ``broker-gateway_default``-Compose-Netzwerk. Datenbasis:
RTH-Boersenstunden, Markt aktiv.

### Bekannte Mitschnitt-Einschraenkung

``CPWebSocketClient`` konsumiert die initialen ``system+success``,
``act`` und ``sts``-Frames waehrend des internen Auth-Waits, **bevor**
die Frames im Recorder ankommen. K4-Mitschnitte enthalten daher kein
Connect-Burst; die initialen Frames sind bereits in der K1-Baseline
dokumentiert. Erstes ``in``-Frame in den K4-JSONLs ist immer schon
das Topic-Update nach Subscribe.

### a) ``smd`` Single-Symbol (AAPL conid 265598)

- 14 ``smd+265598``-Frames in 60 s -> ~1 Update / 4 s.
- Felder im **ersten** Frame: vollstaendig (31, 83, 84, 86, 6509, _updated, 6119, server_id, conid, conidEx, topic).
- Folge-Frames: nur **Delta-Felder** (z.B. nur 84+86 wenn nur Bid/Ask sich geaendert haben). 6509 (DPB-Availability) wird selten neu gesendet, _updated/conid/topic/server_id/6119 immer.
- Werte: Preise 31/84/86 als **String** (`"271.55"`), 83 (% change) als **Float** (0.56). Mixed Types — der Adapter muss pro Feld Zieltyp kennen.
- Keine Pacing-Hinweise.

### b) ``smd`` Multi-Symbol (5 Top-US-Werte)

- 5x Subscribe ``smd+<conid>+{fields}`` direkt hintereinander, Server akzeptiert alle ohne Throttle.
- 98 in-Frames in 60 s, verteilt auf alle 5 Symbole (16-22 Updates / Symbol). Keine sichtbare Reihenfolge-Garantie zwischen Symbolen.
- Frequenz korreliert mit Liquiditaet: META und GOOGL >20 Updates, AAPL nur 16.

### c) ``smd`` Multi-Symbol gross (25 Top-US-Werte)

- 25 Subscribes hintereinander, alle akzeptiert. Conid-Resolve via ``/iserver/secdef/search`` vor dem Subscribe (alle 25 erfolgreich).
- 187 in-Frames in 60 s -> ~3 frames/s aggregiert. Keine Pacing-Violation, kein 429, kein ``message`` mit ``throttle``/``violation``.
- Frequenz pro Symbol stark uneinheitlich: V (80268543) erhielt **80 Updates** allein (sehr hohe Tick-Frequenz), KO (8595) und JNJ (4901) je 4-8.
- Spalte ``server_id``/``6119`` schwankt zwischen Sessions zwischen ``q1`` und ``q32`` - vermutlich IBKR-Quote-Server-ID; nicht stabil ueber Reconnect.

### d) ``sor`` (Live Order Updates) - mit Test-Order

- **Phase 1 (30 s sub ohne Aktion):** 0 sor-Frames. IBKR liefert **keinen** Initial-Snapshot der bestehenden Orders ueber ``sor``. Doku-Aussage "Erste Antwort enthaelt alle aktuellen Orders" stimmt fuer Konten **mit** offenen Orders nicht zwangslaeufig - das Konto war zu dem Zeitpunkt order-frei. Empfehlung Doku: ``/iserver/account/orders`` REST vor Subscribe pollen.
- **Phase 2 (Test-Order):** LMT BUY 1x AAPL @ $1.00 (weit unter Markt). IBKR fragt **drei** Confirmations ab (price-cap 3%, no-market-data, mandatory-cap-price), alle via ``POST /v1/api/iserver/reply/{id} {"confirmed":true}`` bestaetigt. Order erhielt ``order_id=912091175``. Sofortiger ``DELETE /iserver/account/U25235077/order/{id}``.
- **Phase 3 (30 s nach Order-Aktion):** Voller Order-Lifecycle in **<50 ms** ueber 6 sor-Frames:
  1. Vollstaendiger Snapshot (alle Felder: conid, side, orderDesc, status="Inactive", price="1.00", remainingQuantity=1.0, totalSize=1.0, lastExecutionTime, ...).
  2. Mini-Frame nur mit ``conid``+``conidex``+``orderId``+``isEventTrading`` (Marker, keine Status-Aenderung).
  3-5. Status-Deltas: ``PendingSubmit`` -> ``PreSubmitted`` -> ``Submitted``. Jeweils nur ``orderId``, ``status``, ``order_ccp_status``, ``bgColor``/``fgColor`` (UI-Hint).
  6. Snapshot mit ``status="PendingCancel"`` (komplett).
  7. Status-Delta ``status="Cancelled"``.
- **Quirk:** ``timeInForce`` im sor-Frame hiess ``"CLOSE"``, obwohl die Order mit ``"tif":"DAY"`` plaziert wurde - vermutlich IBKR-interne Codierung (DAY ausserhalb RTH wird zu CLOSE). In Adapter-Layer normalisieren.
- **Quirk:** ``bgColor``/``fgColor`` als Hex-Strings - reine UI-Indicator-Felder fuer das offizielle TWS-UI, fuer Trading-Logik irrelevant.

### e) ``str`` (Trades, ``realtimeUpdatesOnly=false``)

- **Initial-Burst:** der erste sub-Frame liefert ``args=[]``, der zweite die komplette Trade-History der letzten 6 Tage. Im Lauf enthielt das ein einzelnes EUR.USD-Forex-Liquidations-Trade vom Vortag.
- **Re-Send statt Delta:** identische ``execution_id`` kommt mehrfach im Stream - einmal ohne ``commission``, danach mit ``commission="0.0"``. IBKR liefert Trade-Updates also als **kompletten Frame**, der Adapter muss auf ``execution_id`` deduplizieren.
- 277 in-Frames in 60 s sind massiv (eigentlich 1 Trade) - das deutet auf einen kontinuierlichen Re-Send-Mechanismus oder einen IBKR-internen Heartbeat-Trigger hin. Bei aktiveren Konten kann der Frame-Strom deutlich groesser werden, das muss der Adapter mit Backpressure handhaben.
- Felder: ``execution_id``, ``conid``, ``conidEx``, ``side`` (S/B 1-Char), ``size`` (float), ``price`` (string!), ``commission`` (string), ``net_amount`` (float!), ``trade_time`` (`YYYYMMDD-HH:MM:SS`), ``trade_time_r`` (unix-ms), ``account``, ``exchange``, ``sec_type`` (STK/CASH/...), ``order_id``, ``execution_id``, ``order_description`` (textuell).

### f) Reconnect mit aktiver Subscription

- Phase 1: subscribe smd+265598, 30 s -> 8 in-Frames. Anschliessend ``CPWebSocketClient.aclose()``.
- 3 s Pause, dann frische ``/tickle``-Session-ID, neuer Connect mit gleicher session-id-Variable - aber der **Server-seitige Subscription-State ist verloren**.
- Phase 2: 30 s Lauschen ohne neuen Subscribe -> **0 smd-Frames**, nur Heartbeats.
- Konsequenz fuer K6: Auto-Reconnect-Logik im WS-Adapter MUSS alle Subscriptions nach jedem Reconnect neu absetzen. CPWebSocketClient (K3) hat den Reconnect, aber kein Subscription-State-Replay - der gehoert in die naechste Schicht.

### Topic - Reife (Ranking-Tabelle)

| Topic | Reife | Begruendung |
|-------|-------|-------------|
| ``smd`` | **gruen** | Felder klar, Subscribe-Format stabil, kein Pacing bei 25 Symbolen. Delta-Verhalten dokumentiert; mixed-type-Werte (string vs. float) sind handhabbar. Erste AP-05-Iteration: nur smd integrieren. |
| ``sor`` | **gruen** | Order-Lifecycle vollstaendig beobachtet, Statuswerte konsistent mit IBKR-Doku. Adapter muss Confirmations-Flow (3 Reply-Schritte) und Delta-vs-Snapshot-Frame-Mix kennen. **Achtung:** Initial-Snapshot ist nicht garantiert - REST ``/iserver/account/orders`` als Bootstrap noetig. |
| ``str`` | **gelb** | Initial-Burst ist gut, aber 277 Frames in 60 s fuer einen historischen Trade ist suspekt - moeglicherweise ein IBKR-Quirk oder eine versteckte Re-Subscription. Vor Adapter-Integration: Mitschnitt mit Konto, das _heute_ einen frischen Trade hatte, wiederholen. ``execution_id``-Dedup im Adapter Pflicht. |
| Reconnect | **rot** | Subscription-State ist verloren - Folge-Layer muss eigenen Subscription-Cache halten und nach jedem ``connect()`` neu auspielen. Kein Server-side-Recovery. |

### Konsequenzen fuer K5 / K6 / AP-05

1. **K5 (Consumer-Fragebogen):** drei konkrete Fragen ableiten.
   (a) Brauchen PSM/trading-robot ``smd`` mit Delta-Updates oder einen normalisierten Quote-Snapshot pro Tick?
   (b) Soll ``sor`` Order-Updates als Deltas (kompakt) oder als materialisierte Snapshots (immer voll) ausgeliefert werden?
   (c) Welche ``str``-Frequenz ist realistisch fuer das Konto - braucht es Sampling/Throttling?
2. **K6 (Architektur-Schnitt):** drei Schichten zwischen ``CPWebSocketClient`` und Consumer-API:
   (a) Subscription-Manager - haelt erwartete Subscriptions, replay nach Reconnect.
   (b) Topic-Adapter - parsed Frame-Format pro Topic (smd/sor/str), normalisiert Mixed-Types, dedupliziert.
   (c) EventBus-Producer - publiziert auf den bestehenden in-process EventBus (v0.11.0).
3. **AP-05 erste Iteration:** nur ``smd`` aufschalten. ``sor``/``str`` warten auf K5-Antworten.

## Offene Fragen fuer spaeter

- Bei ``smd``-Subscribe: kommt das Format dann ``smd+{...}`` oder JSON?
  (Im Spike nicht getestet, weil Subscribe out of scope.)
  **Beantwortet K4:** Subscribe-Frame ist ``smd+<conid>+{json}``,
  Server-Push-Frame ist reines JSON mit ``topic="smd+<conid>"``.
- Verhalten bei ``competing=true`` - wechselt der ``sts``-Frame, oder
  fliegt die WS raus? Wuerde man nur durch parallele Browser-Session
  herausfinden, was hier gegen den Single-Owner-Constraint geht.
- ``ntf`` / ``blt`` - Format und Frequenz; nur unter realer Trade-/
  Bulletin-Last sichtbar.
- ``str``-Frame-Frequenz unter heute-aktivem Konto - die K4-Beobachtung
  von 277 Frames fuer einen Vortags-Trade ist erklaerungsbeduerftig.
