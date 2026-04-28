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

## Offene Fragen fuer spaeter

- Bei ``smd``-Subscribe: kommt das Format dann ``smd+{...}`` oder JSON?
  (Im Spike nicht getestet, weil Subscribe out of scope.)
- Verhalten bei ``competing=true`` - wechselt der ``sts``-Frame, oder
  fliegt die WS raus? Wuerde man nur durch parallele Browser-Session
  herausfinden, was hier gegen den Single-Owner-Constraint geht.
- ``ntf`` / ``blt`` - Format und Frequenz; nur unter realer Trade-/
  Bulletin-Last sichtbar.
