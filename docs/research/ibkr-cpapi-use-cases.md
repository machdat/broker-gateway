# IBKR Client Portal API — Use Cases (Doku-Snapshot 2026-04-25)

Quelle: offizielle IBKR-Doku, vom User waehrend der AP-02 #04 Live-
Recording-Session aus der "Use Cases > Individual"-Sektion uebermittelt.

Diese Datei ist die kanonische Referenz fuer Service-Code-Annahmen
(Pacing, Session, Auth, Combos). Aenderungen an diesen Annahmen MUESSEN
gegen diese Datei abgeglichen sein. Bei spaeterem Refresh
ueberschreiben — Diff per `git log -p` nachvollziehbar.

## Connectivity

- **CP-API vs TWS-API:** CP-API ist webbasiert, Requests gehen ueber
  Internet an IBKR-Backend. Erfordert lokales API-Gateway (oder OAuth /
  dedizierte Verbindung fuer Institutionelle). TWS-API laeuft lokal ueber
  Socket gegen Trader Workstation.

## Session

- **Competing Sessions:** Nur **eine** aktive Brokerage-Session pro
  Username, ueber Client Portal / TWS / IBKR Mobile hinweg. Bei
  `competing: true` koennen wir mit `POST /reauthenticate?force=true`
  konkurrierende Sessions disconnecten und uns neu authentifizieren.
- **Session Duration:**
  - Hartes 24h-Limit. Danach **Browser-2FA-Re-Login** noetig.
  - Idle-Timeout 5 Min wenn keine Requests gehen.
  - Keep-Alive-Empfehlung: `GET /sso/validate` **jede Minute**.
- **Multiple Usernames:** Wer parallel TWS/Mobile/CP-API nutzen will,
  legt einen zweiten Username an
  (siehe IBKR-KB-Artikel). Achtung: Marktdaten-Subscriptions sind
  user-spezifisch und werden separat verrechnet.

## Auth

- **Authenticating without Gateway:** Fuer Individual-Clients **nicht
  moeglich**. Auch nicht in Aussicht.
- **Opting out of 2FA:** Nicht moeglich. CP-API-Login = Client-Portal-Login,
  daher Pflicht-2FA. (Nur einige Trading-Apps wie TWS koennen 2FA in
  bestimmten Setups umgehen — CP-API NICHT.)
- **Automated Login:** Fuer Individual-Clients **nicht supported**. Der
  initiale Login MUSS manuell per Browser durch den End-User. IBKR
  empfiehlt ausdruecklich keine Workarounds und supported sie nicht.
- **Network Error / CORB:** Beim Browser-Test der Doku-Seite muss
  Cross-Origin-Resource-Blocking deaktiviert werden. Postman / Thunder
  Client sind davon nicht betroffen.

## TLS

- **Self-Signed Certificate:** Das CP-Gateway liefert keinen Cert mit.
  Entweder selbst signieren (Tomcat-Style: `*.jks`-Datei in `root/`
  ablegen, `sslCert` und `sslPwd` in der Konfig setzen, neu starten)
  oder offizielles Zertifikat nutzen. Im broker-gateway-Stack ist
  `listenSsl: false` aktiv (HTTP-Transport), Cert-Setup also nicht
  noetig — TLS-Pflicht entstuende erst bei Cross-Host-Deployment.

## Pacing

Hartes Default: **10 req/s**. Endpoint-spezifische Limits ueberschreiben:

| Endpoint | Method | Limit |
|----------|--------|-------|
| `/iserver/marketdata/snapshot` | GET | 10 req/s |
| `/iserver/marketdata/history` | GET | 5 concurrent requests |
| `/iserver/scanner/params` | GET | **1 req / 15 min** |
| `/iserver/scanner/run` | POST | 1 req/s |
| `/iserver/trades` | GET | **1 req / 5 s** |
| `/iserver/orders` | GET | **1 req / 5 s** |
| `/iserver/account/pnl/partitioned` | GET | 1 req / 5 s |
| `/portfolio/accounts` | GET | 1 req / 5 s |
| `/portfolio/subaccounts` | GET | 1 req / 5 s |
| `/pa/performance` | POST | 1 req / 15 min |
| `/pa/summary` | POST | 1 req / 15 min |
| `/pa/transactions` | POST | 1 req / 15 min |
| `/trsv/secdef` | POST | max 200 conids/request |
| `/fyi/...` | div. | meist 1 req/s |
| `/tickle` | GET | 1 req/s |
| `/sso/validate` | GET | 1 req/min |

**Strafe:** HTTP 429 + IP fuer 10 Min im Penalty-Box. Wiederholungstaeter
koennen permanent blockiert werden.

→ **Konsequenzen fuer broker-gateway:**

1. `cp/throttle/manager.py` muss endpoint-spezifische Buckets fuehren,
   nicht nur einen globalen 10/s-Bucket.
2. `/iserver/trades` und `/iserver/orders` (1 req/5s!) brauchen
   Cache-Layer mit min. 5s TTL, sonst ratelimit-Treffer in normalen
   Polling-Loops.
3. `/portfolio/accounts` (1 req/5s) — der noch fehlende `accounts`-Init
   in `cp/lifecycle.py` darf NICHT bei jedem Tickle gerufen werden,
   sondern einmal pro Session cachen.

## Bad Request

Endpoints koennen 400 zurueckgeben, wenn der JSON-Body z.B. CR/LF
enthaelt. Body strippen oder kompakt serialisieren.

## Market Data

- **Snapshot Requests:** Erster Call **erstellt** die Subscription
  (Antwort = Subscription-Details, KEINE Werte). Erst spaetere Calls
  liefern Marktdaten. Bestaetigt damit die First-Call-Prime-Logik im
  bestehenden `cp/quotes.py`.
- **Option Chains:** `strike: 0` setzen, um die volle Option-Chain
  fuer ein Symbol zu bekommen.

## Order Operations

- **Combo Orders:** Multi-Leg via `conidex` statt `conid` im
  `POST /iserver/account/{accountId}/orders`-Body. Format:

  ```
  {spread_conid};;;{leg_conid_1}/{ratio_1},{leg_conid_2}/{ratio_2}
  ```

  - US-Options: `spread_conid = 28812380`.
  - Non-US: `conid@exchange`.
  - Positiver Ratio = Long Leg, negativer = Short Leg.
  - Beispiel-Body (US, 1×Long / 1×Short):

    ```json
    {
      "orders": [{
        "acctId": "DU*******",
        "conidex": "28812380;;;397534457/1,493186808/-1",
        "cOID": "84401484",
        "orderType": "LMT",
        "listingExchange": "",
        "outsideRTH": false,
        "price": 1.25,
        "side": "BUY",
        "ticker": "",
        "tif": "DAY",
        "referrer": "NO_REFERRER_PROVIDED",
        "quantity": 1,
        "isClose": false
      }]
    }
    ```

  → Folgekarte `569d66ff` im Backlog.

## Account Data & Reports

- **Portfolio Discrepancies:** Daten ueber CP-API koennen wegen
  Filterunterschieden leicht von Statements / Flex-Queries abweichen.
  Fuer offizielle Reports sind Statements oder Flex-Queries die
  Wahrheit, nicht die CP-API-Antworten.
- **Flex Queries:** Komplexe Reports werden ueber das **Flex Web
  Service API** geladen (separate IBKR-API, HTTPS). CP-API liefert
  diese Daten nicht.
- **Order Updates** (`/iserver/account/orders`): wie Market-Data-Snapshot
  zwei-stufig. Erster Call erstellt Subscription, zweiter liefert die
  Daten. **Bis 5 Sekunden** zwischen den Calls einplanen, damit die
  Subscription serverseitig steht.

  → Bestaetigt: `cp/orders.py` fuer Order-Status braucht ggf. einen
  Subscription-Erst-Aufruf plus min. 5s Wartezeit, falls die Single-
  Status-Variante (`/iserver/account/order/status/{orderId}`) das
  nicht abdeckt. Verifikation in der Folgekarte 813fed62.
