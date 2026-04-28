# Runbook: Recording-Session Happy-Path (AP-02 #04)

Live-Aufzeichnung aller v1-Happy-Path-Endpunkte des CP-Gateways gegen
das Konto **U25235077** (Non-Pro AT). Erzeugt JSON-Fixtures unter
``tests/fixtures/recorded/live/`` und ein begleitendes Manifest.

## Voraussetzungen

1. **CP-Gateway-Container** laeuft auf cma-pi-1 und ist gesund:

   ```bash
   ssh cma@cma-pi-1 'docker ps --filter name=broker-cpgateway'
   ssh cma@cma-pi-1 'docker logs --tail 20 broker-cpgateway 2>&1 | tail'
   ```

2. **Browser-Login frisch** (Session lebt typischerweise ~ 8h):

   - SSH-Reverse-Tunnel offen: `ssh -L 5000:localhost:5000 cma@cma-pi-1`.
   - Im Browser https://localhost:5000 oeffnen, Username +
     Initial-Passwort eingeben, danach 2FA per IBKey-App bestaetigen.
   - Vollstaendige Anleitung: ``docs/runbooks/cpgateway-login.md``.

3. **Lokales venv aktiviert** mit installierten dev-Dependencies
   (``pip install -e .[dev]``). Das Skript braucht keinen Container,
   es spricht direkt ueber den SSH-Tunnel mit dem CP-Gateway.

4. **Boersenoeffnungszeiten** im Blick behalten - Variante B
   (Place + sofort Cancel) sollte ausserhalb der NASDAQ/NYSE-Zeiten
   laufen, damit eine versehentliche Order nicht ausfuehrt.

## Vorabtest gegen Tunnel

```bash
curl -s http://localhost:5000/v1/api/iserver/auth/status | jq
```

Erwartete Antwort:

```json
{
  "authenticated": true,
  "competing": false,
  "connected": true,
  "MAC": "...",
  "userId": 123456
}
```

Wenn ``authenticated=false`` oder HTTP 401: Browser-Login wiederholen.

## Recording-Lauf

### Variante A: nur Preview-Order (whatif)

```bash
python scripts/recording_session.py happy-path \
    --record-dir tests/fixtures/recorded/live \
    --base-url http://localhost:5000/v1/api \
    --account-id U25235077 \
    --symbols AAPL MSFT SAP
```

Das Skript fragt vor dem whatif-Aufruf:

```
[run] j) POST /iserver/account/U25235077/orders/whatif (preview)
  Variante A (whatif Preview) ausfuehren? [yes/no, default no]:
```

`yes` antworten, sobald gewuenscht. Bei `no` wird der Schritt
uebersprungen (alle anderen Endpunkte werden trotzdem aufgezeichnet).

### Variante B: zusaetzlich live Place + Cancel

```bash
python scripts/recording_session.py happy-path \
    --record-dir tests/fixtures/recorded/live \
    --base-url http://localhost:5000/v1/api \
    --account-id U25235077 \
    --symbols AAPL MSFT SAP \
    --with-place-cancel
```

Skript fragt sowohl vor whatif als auch vor place+cancel separat. Die
gewaehlte Limit-Order (Limit 1.00 USD bei AAPL, weit unter Markt)
fuehrt unter normalen Marktbedingungen nicht aus, wird aber sicherheits-
halber sofort wieder gecancelt und der Cancelled-Status verifiziert.

### Skript-Flags

| Flag | Wirkung |
|------|---------|
| `--record-dir` | Ziel fuer JSON-Fixtures (default ``tests/fixtures/recorded/live``). |
| `--base-url` | CP-Gateway-Endpoint inkl. ``/v1/api``-Prefix. |
| `--account-id` | IBKR-Konto (default ``U25235077``). |
| `--symbols` | Symbole fuer secdef + snapshot (default AAPL MSFT SAP). |
| `--skip-orders` | Ueberspringt whatif und place+cancel komplett. |
| `--with-place-cancel` | Aktiviert Variante B. |
| `--yes` | Konsolen-Abfragen unterdruecken (CI/Skript-Modus). |
| `--normalize-prices` | Auch Preise/Marktdaten durch Platzhalter ersetzen. |

## Nach dem Lauf

1. **Recordings inspizieren:**

   ```bash
   ls tests/fixtures/recorded/live/
   cat tests/fixtures/recorded/live/live-recording-manifest.json
   ```

   Pruefen, dass die Anzahl Files zur Schritt-Liste passt
   (12 Pflicht + ggf. 4 fuer place/cancel).

2. **Geheimnis-Check:**

   ```bash
   grep -ri "Bearer\|Cookie:" tests/fixtures/recorded/live/
   ```

   Muss leer sein. Sollte etwas auftauchen, betroffene Datei loeschen
   und den Recorder-Filter (``_REDACTED_HEADERS_LOWER`` in
   ``src/broker_gateway/cp/recorder.py``) erweitern, dann erneut laufen
   lassen.

3. **Diff-Bewertung seed vs. live:**

   ```bash
   diff -ru tests/fixtures/recorded/seed/ tests/fixtures/recorded/live/
   ```

   Pro Endpunkt notieren:
   - Welche Felder hat live, die seed nicht hatte? -> ggf. additive
     Anpassung im Service-Code (normalize/quotes/availability/money/
     order_models). Nur Adapter-Schicht aendern, keinen v1-Vertrag
     brechen.
   - Welche Felder hat seed, die live nicht liefert? -> seed war
     unrealistisch, anpassen oder loeschen.
   - Werte-Klassen, die der Recorder noch nicht normalisiert hat
     (z.B. neue Timestamp-Variante)? -> normalize.py erweitern.

4. **Test-Suite mit live-Recordings ausfuehren:**

   ```bash
   pytest
   ```

   Sollte komplett gruen bleiben. Falls ein Test bricht, weil ein
   live-Recording einen anderen Body als seed liefert, ist das per
   Definition richtig (Realitaet > seed) - Test additiv anpassen,
   ohne die Test-Logik zu verbiegen.

5. **Commit:**

   - Recordings + Manifest + Diff-Report committen.
   - CHANGELOG-Eintrag fuer 1.3.0 mit Datum des Recording-Laufs.

## Wiederholtes Aufzeichnen

Sobald IBKR ein Schema-Detail aendert oder neue Felder liefert, das
Skript einfach erneut ausfuehren - der Recorder ueberschreibt die
bestehenden Files (gleicher Pfad+Method+Query => gleicher Filename).
Die Diff-Bewertung im naechsten Lauf zeigt, was sich geaendert hat.

Empfohlene Cadence:
- Nach jedem CP-Gateway-Update (``ops/cpgateway/clientportal.gw.tar.gz``).
- Sobald Tests ohne erkennbaren Code-Aenderung beginnen, inkonsistent
  mit dem Live-Verhalten zu wirken.
- Vor einem groesseren Service-Release als Sanity-Check.

## Diff-Report 2026-04-28 (AP-02 #07-4: korrigierte Pfade)

Re-Recording nach Abschluss von AP-02 #07-1/2/3 (Service-Code an reale
IBKR-Pfade angeglichen, Lifecycle-Hooks ergaenzt). 23 Recordings unter
`tests/fixtures/recorded/live/`, davon 16 HTTP 200 fuer alle
v1-Service-Pfade, 7 dokumentarische 404er.

### A) Service-Code-Korrekturen verifiziert (alle HTTP 200)

| Endpoint | Karte | Status |
|----------|-------|--------|
| `GET /portfolio/U25235077/summary` | 07-1 | 200 ✓ |
| `GET /portfolio/U25235077/positions/0` | 07-1 | 200 ✓ |
| `GET /portfolio/U25235077/ledger` | 07-1 | 200 ✓ |
| `GET /iserver/account/trades?days=7` | 07-2 | 200 (Body mit `account`+`listing_exchange`) ✓ |
| `GET /iserver/accounts` | 07-3 | 200 ✓ |
| `GET /sso/validate` | 07-3 | 200 (`RESULT=true`) ✓ |
| `POST /iserver/account/U25235077/orders/whatif` | (Folge) | 200 ✓ |

Order-Status-Singular `GET /iserver/account/order/status/{orderId}` ist
in der Happy-Path-Sequenz nicht enthalten (waere ein Live-Order ueber
Variante B noetig); der Pfad ist im Error-Path-Recording (07-4-Vorlauf,
existing) mit HTTP 503 "Order not found" verifiziert.

### B) Synthetische Seeds aus 07-1/2/3 entfernt

Folgende Seeds wurden geloescht, weil reale Live-Recordings existieren:

- `portfolio_U25235077_summary__GET__noquery_01.json` (07-1)
- `portfolio_U25235077_positions_0__GET__noquery_01.json` (07-1)
- `portfolio_U25235077_ledger__GET__noquery_01.json` (07-1)
- `iserver_accounts__GET__noquery_01.json` (07-3)
- `sso_validate__GET__noquery_01.json` (07-3)

### C) Recorder-Filter erweitert (Geheimnis-Schutz)

Erstes 07-4-Recording hat einen Auth-Token im `sso/validate`-Body
geleakt (`TOKEN`, `CREDENTIAL`, `IP`, `USER_NAME`, `USER_ID`,
`UNIQUE_LOGIN_ID`, `MAC` aus `auth/status`, `userId` aus `tickle`). Der
Body-Normalizer in `src/broker_gateway/cp/normalize.py` ist um
`_SECRET_FIELDS_LOWER` erweitert; alle Werte werden auf `<REDACTED>`
gesetzt, das Schema bleibt fuer Drift-Check / Replay sichtbar.

### D) IBKR-Server-Build-Drift

Live-Server: `JifZ20074` Build `10.45.1a` (vorher: `JifZ28031` Build
`10.44.1h`). MAC- und Server-Felder sind redacted, Drift bleibt nur als
additiver Schema-Indikator sichtbar.

## Diff-Report 2026-04-25 (erster Live-Lauf)

Abgeglichen wurden 22 Recordings (16 HTTP 200, 5 HTTP 404, 1 mit reichem Body)
gegen die seed-Defaults und gegen die offizielle IBKR-Endpunkt-Doku
(`docs/research/ibkr-cpapi-doc.json`, gefetcht von
<https://www.interactivebrokers.com/api/doc.json>).

### A) Service-Code-Bugs (Pfade falsch — Folgekarte)

Drei Service-Methoden senden gegen Pfade, die in der IBKR-Doku **gar
nicht existieren**. In Production wuerden sie genauso 404 liefern wie im
Live-Recording.

| Service | falscher Pfad | korrekter Pfad (laut Doku) |
|---------|---------------|----------------------------|
| `cp/portfolio.py::summary` | `/iserver/account/{acct}/portfolio` (gibt es nicht) | `/portfolio/{acct}/summary` |
| `cp/portfolio.py::positions` | `/iserver/account/{acct}/positions` (gibt es nicht) | `/portfolio/{acct}/positions/{pageId}` (Pagination!) |
| `cp/portfolio.py::ledger` | `/iserver/account/{acct}/ledger` (gibt es nicht) | `/portfolio/{acct}/ledger` |
| `cp/orders.py::status (Z. 95)` | `/iserver/account/orders/{orderId}` (Bulk-Live-Orders, kein Single) | `/iserver/account/order/status/{orderId}` |

Zusaetzlich fehlt im Service-Lifecycle der **Server-Side-Init**
`GET /iserver/accounts` — IBKR antwortet auf account-spezifische Calls
sonst mit HTTP 404, selbst wenn der Pfad korrekt ist. Der Tickle-Job in
`cp/lifecycle.py` sollte einmal pro frischer Session `GET /iserver/accounts`
absetzen.

→ Folgekarte: **Portfolio-Pfade umstellen + accounts-Init im Lifecycle**.

### B) Schema-Diffs (additive Adapter — zum Teil schon gefixt)

| Endpunkt | seed-Schema | live-Schema | Bewertung |
|----------|-------------|-------------|-----------|
| `secdef/search` | 1 Eintrag, `secType` am Top-Level | mehrere Listings (NASDAQ/TSE/MEXI/EBS/BOND), `sections[].secType`, `companyHeader`, `restricted` | **gefixt in `cp/instruments.py`**: `_map_search_entry` liest `sections[0].secType` als Fallback; `search()` filtert auf primary STK |
| `secdef/info` | `symbol`, `exchange` | `ticker`, `listingExchange` (z.B. "NASDAQ.NMS"), `validExchanges`, `priceRendering`, `maturityDate`, `right`, `strike` | **gefixt in `cp/instruments.py`**: `_map_info` nimmt `ticker` als symbol-Fallback und `listingExchange` als exchange-Fallback |
| `tickle` | flache Felder | zusaetzlich `iserver.authStatus.{authenticated, established, competing, connected, MAC, serverInfo}` und `hmds: {error}` | additive Felder, Service ignoriert sie |
| `auth/status` | `MAC: "MOCKED"` | echte MAC `06:59:AE:5B:4F:D1`, `serverInfo: {serverName, serverVersion}`, `established`, `hardware_info` | additive Felder, Tests pruefen jetzt nur Existenz statt konkreten Wert |
| `marketdata/snapshot` | First-Call-Prime (leer) → Second-Call (Werte) | Felder: `conid` (int), `conidEx` (string), `_updated`, `6119`, `server_id`, `31/84/86`, `6509: "ZB"` (statt "DPB"), `6508` mit `serviceID*`-Liste. **First-Call-Prime ist laut IBKR-Doku doch korrekt modelliert** — der erste Call erstellt die Snapshot-Subscription, erst spaetere Calls liefern Daten. Im Recording-Lauf erschien das nicht, weil die Subscriptions vom vorherigen Lauf serverseitig noch aktiv waren. Anpassung: `cp/quotes.py` Logik bleibt, aber Availability-Code `ZB` muss in `availability.py` aufgenommen werden. |
| `account/trades` | 7 Felder pro Trade (`account_id`, `currency`, ...) | 27 Felder (`account` statt `account_id`, `accountCode`, `clearing_id`, `clearing_name`, `company_name`, `contract_description_1`, `exchange`, `is_event_trading`, `liquidation_trade`, `listing_exchange`, `order_description`, `position`, `sec_type`, `supports_tax_opt`, `trade_time_r` ...). **Kein `currency`-Feld direkt!** | Folgekarte: `cp/trades.py` muss `account` mappen und Currency anders ableiten (z.B. aus `listing_exchange`). |
| `orders/whatif` | n/a (war bisher nicht im Mock) | Object (nicht Liste!) mit `amount/equity/initial/maintenance/position/warn`. `warn` ist ein HTML-Snippet, das per `/iserver/reply/{id}` bestaetigt werden muss. | Service-Code in `cp/orders.py` (whatif noch nicht implementiert) muss mit Object + Warning-Confirmation umgehen. |

### C) HTTP-404-Recordings (nicht Service-Bug, sondern korrekt)

| Endpunkt | Status | Bewertung |
|----------|--------|-----------|
| `/iserver/marketdata/{cid}/unsubscribe` | 404 | **Pfad ist laut Doku korrekt** (Market Data Cancel Single). 404 ist Live-Artefakt: keine aktive Subscription oder IBKR-Wartung. Service-Code-Pfad bleibt. |

### D) Verifikationen aus der Doku

| Service-Pfad | Doku-Status |
|--------------|-------------|
| `POST /iserver/account/{acct}/orders` (Place) | ✓ vorhanden ("Place Orders") |
| `POST /iserver/account/{acct}/orders/whatif` (Preview) | ✓ vorhanden ("Preview Orders") |
| `DELETE /iserver/account/{acct}/order/{id}` (Cancel) | ✓ vorhanden — beachten: **Singular** `/order/`, nicht `/orders/` |
| `POST /iserver/reply/{id}` (Reply-Confirmation) | ✓ vorhanden |
| `GET /iserver/marketdata/snapshot` | ✓ vorhanden |
| `GET /iserver/marketdata/{conid}/unsubscribe` | ✓ vorhanden |
| `GET /iserver/marketdata/unsubscribeall` | ✓ vorhanden — sollte als Cleanup-Endpunkt im Service ergaenzt werden |
| `GET /iserver/secdef/search` und `/info` | ✓ vorhanden |
| `POST /tickle` und `POST /reauthenticate` und `GET /iserver/auth/status` | ✓ vorhanden |
| `GET /iserver/accounts` | ✓ vorhanden — **fehlt im Service-Lifecycle als Pflicht-Init!** |

### Was wurde in dieser Karte direkt gefixt

- `src/broker_gateway/cp/instruments.py`: `_map_search_entry` liest `sections[0].secType`; `_map_info` nimmt `ticker` und `listingExchange` als Fallbacks; `search()` filtert auf primary STK-Listing.
- 3 Tests gelockert auf strukturelle Pruefung statt konkreten Wert (tickle session, replay-loader MAC, instruments exchange "NASDAQ.NMS"-Toleranz).
- `tests/cp_mock/loader.py`: live-Recording mit HTTP 4xx/5xx faellt auf seed zurueck — schuetzt Tests vor 404-Beweis-Recordings.
- 22 Recordings unter `tests/fixtures/recorded/live/` + Manifest.
- `docs/research/ibkr-cpapi-doc.json` (170 KB Swagger-Snapshot) eingecheckt.

### Was bleibt fuer Folgekarte(n)

1. **Portfolio-Service-Code umstellen** auf `/portfolio/{acct}/{summary,positions/0,ledger}` inkl. Money-Format-Mapping (`{amount, currency, isNull, timestamp, value, severity}`) und Positions-Pagination.
2. **`/iserver/accounts`-Init** in `cp/lifecycle.py::_mark_session_ok` einmalig pro frischer Session aufrufen.
3. **Order-Status-Pfad fixen**: `/iserver/account/order/status/{orderId}` (Singular + `status/`) in `cp/orders.py:95`.
4. **`cp/trades.py`**: Mapping `account → account_id`, currency-Inferenz aus `listing_exchange`, Felder additiv aufnehmen (`accountCode`, `clearing_*`, `liquidation_trade`).
5. **`cp/quotes.py`**: First-Call-Prime ist laut IBKR-Doku korrekt — der erste Call erstellt die Snapshot-Subscription, erst Folge-Calls liefern Daten. Service-Code bleibt; nur Availability-Code `ZB` in `availability.py` aufnehmen.
6. **whatif-Endpoint** im Service-Code implementieren (Object-Response mit warn-Confirmation-Loop).
7. **`/iserver/marketdata/unsubscribeall`** als Cleanup-Endpunkt aufnehmen.
8. **Live-Recording-Lauf erneut** nach Service-Code-Fixes, um zu pruefen, dass `/iserver/account/.../portfolio` weg ist und alle Recordings 200 OK sind.
9. **Session-Lifecycle gegen IBKR-Doku abgleichen** (Quelle: IBKR Quickstart, vom User aus der offiziellen Doku zitiert):
   - Hartes 24h-Limit: nach 24h authentifizierter Session muss der User per Browser-2FA re-authentifizieren. Service sollte das proaktiv signalisieren (z.B. `last_login_at` im `/v1/internal/health`, Warnung ab Stunde 23).
   - Idle-Timeout 5 Min: wenn keine Requests gehen, kippt die Session. Tickle-Default 60s schuetzt davor, aber **IBKR empfiehlt explizit `GET /sso/validate` jede Minute** statt `POST /tickle` — `cp/lifecycle.py` sollte den Keep-Alive auf `/sso/validate` umstellen oder beides parallel laufen lassen.
   - Re-Auth-Pfad nach Idle-Timeout: noch nicht im Code abgedeckt, evtl. eigene Folgekarte.
   - **Architektur-Constraint (IBKR-Doku):** Automated Login wird fuer Individual Clients **nicht** unterstuetzt. Der initiale Login MUSS vom End-User per Browser-2FA erfolgen — kein Service-seitiges Auto-Login moeglich. Bestaetigt damit die bestehende Strategie: broker-gateway haelt nur eine bereits manuell etablierte Session warm und signalisiert bei Verlust ein hartes `auth_lost`, damit der User bewusst neu einloggt. Workarounds werden von IBKR explizit nicht supported.
   - **Competing Sessions (IBKR-Doku):** Pro Username darf nur **eine** Brokerage-Session aktiv sein, ueber Client Portal / TWS / IBKR Mobile hinweg. Bei `competing: true` bietet `/reauthenticate?force=true` an, andere Sessions zu disconnecten. Der aktuelle Service-Code in `cp/lifecycle.py` ruft `/reauthenticate` ohne `force` auf — die Folgekarte sollte einen Schalter einbauen, damit broker-gateway als Single-Owner (siehe Memory `project_ibkr_session_owner.md`) konkurrierende Sessions aktiv zurueckerobern kann. Default sollte trotzdem `force=false` sein, damit wir nicht versehentlich eine User-getriebene Mobile-App-Session kicken.
