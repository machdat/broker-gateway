# IBKR Client Portal API — Websockets (Doku-Snapshot 2026-04-25)

Quelle: offizielle IBKR-Doku, vom User waehrend AP-02 #04 uebermittelt.
Kanonische Referenz fuer einen kuenftigen WebSocket-Adapter im
broker-gateway. Bei spaeterem Refresh ueberschreiben.

## Verbindung

- **URL:** `wss://localhost:5000/v1/api/ws` (HTTPS-WebSocket, basiert
  auf demselben CP-Gateway, der auch REST liefert).
- **Voraussetzung:** Brokerage-Session ist bereits per Browser-Login
  authentifiziert (via REST `/iserver/auth/status` pruefbar).
- **Auth nach Verbindung:**
  - Browser uebernimmt Cookies aus `Set-Cookie` automatisch.
  - Client-App schickt erste WS-Message mit der `sessionId` aus dem
    REST-Endpoint `/tickle`:

    ```json
    { "session": "sessionId" }
    ```

- **Erfolgs-Antwort:** `topic=sts` mit `args.authenticated=true`.

## Message-Format

```
TOPIC+{ARGUMENTS}
```

- `TOPIC` = 3 Zeichen, beginnt mit `s` (subscribe) oder `u`
  (unsubscribe). System-/State-Messages haben eigene Topics.
- `+` = Trennzeichen.
- `{ARGUMENTS}` = JSON-Object, optional `{}`.

## Solicited Topics

| Subscribe | Unsubscribe | Was |
|-----------|-------------|-----|
| `smd+{conid}+{fields:[...]}` | `umd+{conid}+{}` | Streaming Market Data (Top-of-Book L1). Felder wie bei `/marketdata/snapshot` (31=last, 84=bid, 86=ask, 6509=availability, 83=daily%change, ...). Default SMART-Routing; `conid@EXCHANGE` zwingt eine Exchange. |
| `smh+{conid}+{params}` | `umh+{serverId}` | Streaming Historical Data. Params: `exchange`, `period` (1-30min/1-8h/1-1000d/1-792w/1-182m/1-15y), `bar` (1min..1m), `outsideRTH`, `source` (midpoint/trades/bid_ask/bid/ask), `format` mit `%o/%c/%h/%l/%v`. **Max 5 concurrent**. |
| `sbd+{acctId}+{conId}+{exchange}` | `ubd+{acctId}` | Market Depth (Deep Book). Optional Exchange. |
| `sor+{}` | `uor+{}` | Live Order Updates. Erste Antwort enthaelt alle aktuellen Orders, danach nur Deltas (Status, partial fills). Empfohlen: `/iserver/account/orders` REST einmal vor Subscribe ziehen. |
| `str+{...}` | `utr` | Trades (heute + 6 Vortage). Optional `realtimeUpdatesOnly:true` oder `days:1..7`. Liefert auch Updates fuer neue Trades. |
| `spl+{}` | `upl+{}` | P&L Updates pro Account: `dpl` (daily) + `upl` (unrealized). Update-Frequenz max 1/s waehrend Marktoeffnung. |
| `tic` | — | Ping fuer Session-Keep-Alive (mind. 1/min empfohlen). **Ersetzt NICHT** `/tickle` REST — das muss zusaetzlich, sobald `/sso/validate` einen 0 liefert. |

## Unsolicited Topics (vom Server gepusht)

| Topic | Wann | Inhalt |
|-------|------|--------|
| `system` | bei Connect, dann Heartbeat alle 10s | `success: <username>` oder `hb: <unix-ms>` |
| `sts` | bei Connect + bei Auth-Aenderung (z.B. competing session) | `authenticated: bool` + Status-Felder |
| `act` | bei Connect + bei Account-Aenderungen | Account-Liste + `acctProps`, `aliases`, `allowFeatures`, `chartPeriods`, `selectedAccount`, `serverInfo`, `sessionId`, `isFT`, `isPaper` — sehr aehnlich zur REST-Antwort `/iserver/accounts` |
| `ntf` | bei Trade-Notification | `id`, `text`, `title`, `url` |
| `blt` | bei IBKR-Bulletin (Exchange-Issues, System-Probleme) | `id`, `message` |

## Beispiele

### Marktdaten fuer AAPL (Close-Preis + daily% change)

```
smd+265598+{"fields":["31","83"]}
```

### Historische Bars (open/close/high/low, 1d, 1h-Bars)

```
smh+265598+{"period":"1d","bar":"1h","source":"trades","format":"%o/%c/%h/%l"}
```

### Realtime-only Trades

```
str+{"realtimeUpdatesOnly":true}
```

## Was das fuer broker-gateway bedeutet

Die WebSocket-Schicht des CP-Gateways adressiert direkt zwei
bestehende Schmerzpunkte:

1. **Quotes-Polling abloesen.** `cp/quotes.py` polled aktuell
   `/iserver/marketdata/snapshot` (Pacing 10/s pro IP). Mit `smd`
   gibt's Push-Updates ohne Polling-Pacing-Druck — relevant sobald
   trading-robot mehrere Symbole gleichzeitig beobachten will.
2. **Order-Status-Polling abloesen.** `/iserver/account/orders` ist
   `1 req/5s` pacing-limitiert. `sor` liefert Status-Deltas Push-
   basiert — der bestehende `EventBus` (v0.11.0) waere ein
   natuerlicher Konsument.

Die Anforderungen an Single-Owner-Session bleiben (eine WS pro
Session), die `tic`-Pings ersetzen NICHT `/tickle` REST sondern
ergaenzen es. Cookie-Auth aus REST-Session ist Voraussetzung —
broker-gateway haette also einen REST-Login-Vorlauf, dann WS-Upgrade
mit Cookie-Reuse.

→ Backlog-Karte fuer das eigene AP-X (Karten-ID nach Anlage).
