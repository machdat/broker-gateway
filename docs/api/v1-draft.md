# broker-gateway API v1 — Working Draft

**Status:** Draft. Wird im Rahmen von AP-01 (KanPrompt-Projekt `broker-gateway`) iterativ konsolidiert. Jede Implementierungs-Karte aktualisiert die zugehörigen Abschnitte.
**Stand:** 2026-04-25 (Service-Version 0.5.0)

> ⚠ **Hinweis für Consumer:** Bis v1.0.0 freigegeben ist, ist diese Spezifikation nicht stabil. Consumer-Implementierungen sollten erst nach formaler v1.0.0-Markierung beginnen.

### Implementation Status (Service-Version 0.5.0)

| Section | Implementiert |
|---|---|
| 3.1 Public Health (`GET /v1/health`) | ✅ in v0.1.0 |
| 1.3 Authentifizierung, 1.4 Scopes | ✅ in v0.3.0 |
| 2.1 Token erstellen (`POST /v1/auth/token`) | ✅ in v0.3.0 |
| 2.2 Token revoken (`DELETE /v1/auth/token`) | ✅ in v0.3.0 |
| 3.2 Internal Health (`GET /v1/internal/health`) | ✅ in v0.4.0 (vereinfachter Body, siehe Section 3.2) |
| Auth-Lifecycle (Tickle + Reauth + 503-Guard) | ✅ in v0.4.0 |
| 4.1 Search (`GET /v1/instruments/search`) | ✅ in v0.5.0 (vereinfachter Body, siehe Section 4.1) |
| 4.2 Detail (`GET /v1/instruments/{conid}`) | ✅ in v0.5.0 (vereinfachter Body, siehe Section 4.2) |
| 1.6 Error-Modell (Schema mit `error.code`/`error.message`) | ⏳ aktuell FastAPI-Default `{"detail": "..."}` |
| Alle anderen | ⏳ Folgekarten in AP-01 |

Die Beispiel-Response in Section 3.1 zeigt die geplante v1.0-Form. In der aktuell ausgelieferten v0.5.0 ist `version` die Service-Version `0.5.0` (Single Source of Truth: `pyproject.toml` + `broker_gateway.__version__`).

---

## 1. Gemeinsamkeiten

### 1.1 Base URL

```
https://<gateway-host>/v1
```

`<gateway-host>` ist im PSM-Stack typischerweise `broker-gateway:4000` (intern) oder `broker-gateway.internal` über Reverse-Proxy.

### 1.2 Versionierung

- **Pfad-basiert.** `/v1/...` bleibt rückwärtskompatibel, solange die Major-Version unterstützt wird.
- **Additive Felder erlaubt.** Neue Felder in Responses sind keine Breaking-Change.
- **Semantische Änderungen** (Default-Werte, Error-Codes, Retry-Logik) erfordern eine neue Major-Version.
- Bei Deprecation einer Major-Version setzt der Service den Header `Deprecation: true` und `Sunset: <RFC7231-Datum>`.

### 1.3 Authentifizierung

Alle Endpoints (außer `/v1/auth/token` und `/v1/health`) verlangen einen API-Token im Header:

```
Authorization: Bearer <token>
```

Tokens sind opake Strings, ausgegeben über `/v1/auth/token`. Sie tragen Scope-Claims, die der Server bei jedem Request prüft. Fehlt ein erforderlicher Scope, antwortet der Server mit `403 Forbidden` und Body:

```json
{
  "error": {
    "code": "missing_scope",
    "message": "Required scope: orders:write",
    "required_scope": "orders:write"
  }
}
```

### 1.4 Scopes

Single Source of Truth ist `src/broker_gateway/auth/models.py`. Aktuell definiert sind genau diese sechs Scopes:

| Scope | Berechtigt zu |
|---|---|
| `instruments:read` | Symbol-Lookup, conid-Lookup |
| `quotes:read` | Snapshots + Streams |
| `portfolio:read` | Portfolio + Positions + Ledger + Trade-Historie |
| `orders:write` | Orders platzieren / canceln + Order-Status abfragen |
| `events:read` | Events-Stream (Execution-Reports etc.) |
| `admin:*` | Token-Verwaltung, Service-Internals; passt automatisch alle Scope-Checks |

Weitere Splits (z.B. `orders:read` separat von `orders:write` oder `trades:read` separat von `portfolio:read`) sind bewusst nicht vorgesehen — Granularität wird erst eingeführt, wenn ein Consumer sie tatsächlich braucht.

### 1.5 Idempotency

Schreib-Endpoints (`POST /v1/orders`, `DELETE /v1/orders/{id}`) verlangen einen Header:

```
Idempotency-Key: <client-generated UUID>
```

Server speichert `key → response` für 24 h. Wiederholte Requests mit demselben Key liefern die ursprüngliche Response — kein erneuter Broker-Call.

### 1.6 Error-Modell

Alle Fehler liefern HTTP-Status + JSON-Body:

```json
{
  "error": {
    "code": "<machine-readable code>",
    "message": "<human-readable message>",
    "details": { ... },
    "retry_after": 30
  }
}
```

| Status | Code-Beispiele | Bedeutung |
|---|---|---|
| 400 | `invalid_request`, `invalid_symbol` | Client-Fehler |
| 401 | `missing_token`, `invalid_token` | Auth-Fehler |
| 403 | `missing_scope`, `account_not_authorized` | Permission-Fehler |
| 404 | `not_found` | Ressource existiert nicht |
| 409 | `idempotency_conflict` | Idempotency-Key wurde mit anderem Body verwendet |
| 422 | `validation_failed`, `order_rejected` | Request semantisch ungültig oder Broker-Reject |
| 429 | `rate_limit_exceeded` | Throttle griff |
| 503 | `broker_unavailable`, `auth_session_expired` | IBKR-Seite hat Probleme oder Re-Login nötig |

### 1.7 Pagination

Listen-Endpoints unterstützen Cursor-Pagination:

- Request: `?limit=100&cursor=<opaque>`
- Response: `{ "items": [...], "next_cursor": "..." | null }`

### 1.8 Datum / Zeit

Alle Zeitstempel als ISO-8601 mit Timezone (UTC bevorzugt):

```
2026-04-24T17:30:00Z
```

### 1.9 Geld-Beträge

Alle Geld-Felder als Objekt:

```json
{ "value": "274.06", "currency": "USD" }
```

`value` ist String (verhindert Float-Rounding). Currency ist ISO-4217.

---

## 2. Authentifizierung

Tokens sind **opake Strings** (kein JWT), serverseitig generiert über `secrets.token_urlsafe(32)` und im Token-Store persistiert. Backend-Auswahl per ENV:

- ohne `BG_TOKEN_FILE` → `InMemoryTokenStore` (Default; State geht beim Neustart verloren — das ist Absicht, der Service ist transient).
- mit `BG_TOKEN_FILE=/path/to/tokens.json` → `FileTokenStore` (atomarer Write via temp-file + rename).

Beim Start liest die App optional `BG_BOOTSTRAP_ADMIN_TOKEN`; ist die Variable gesetzt und das Token noch nicht im Store, wird es als Token mit `caller_id=bootstrap-admin` und Scopes `[admin:*]` registriert. So kann initial mindestens ein Admin-Aufruf erfolgen, ohne dass weitere Tokens im Repo oder Compose-File liegen.

### 2.1 Token erstellen

```
POST /v1/auth/token
Authorization: Bearer <admin-token>          ← muss admin:* tragen
Content-Type: application/json

{
  "caller_id": "psm",
  "scopes": ["quotes:read", "portfolio:read", "instruments:read"],
  "expires_at": "2027-04-24T17:30:00Z"        ← optional
}
```

Response `201 Created`:

```json
{
  "value": "RANDOM_OPAQUE_TOKEN_VALUE",
  "caller_id": "psm",
  "scopes": ["quotes:read", "portfolio:read", "instruments:read"],
  "created_at": "2026-04-24T17:30:00.123Z",
  "expires_at": "2027-04-24T17:30:00Z"
}
```

Fehlerfälle:

| Status | Bedingung |
|---|---|
| `401 Unauthorized` | kein/ungültiges Bearer-Token |
| `403 Forbidden` | aufrufendes Token hat nicht `admin:*` |
| `422 Unprocessable Entity` | unbekannte Scope-Namen oder leere `caller_id` |

### 2.2 Token revoken

```
DELETE /v1/auth/token
Authorization: Bearer <token>
DELETE /v1/auth/token?value=<other-token>     ← admin:* nötig
```

Ohne Query-Parameter `value` revoket der Aufruf das eigene Bearer-Token (Self-Revoke, kein admin:* nötig). Wird ein fremdes Token-Value angegeben, ist `admin:*` Pflicht.

Response `204 No Content`.

Fehlerfälle:

| Status | Bedingung |
|---|---|
| `401 Unauthorized` | kein/ungültiges Bearer-Token |
| `403 Forbidden` | fremdes Token ohne `admin:*` |
| `404 Not Found` | Token-Value existiert nicht (mehr) |

---

## 3. Health

### 3.1 Public Health

```
GET /v1/health
```

Kein Auth nötig. Response 200:

```json
{
  "status": "ok",
  "version": "0.4.0"
}
```

`version` ist die Service-Version (`broker_gateway.__version__`).

### 3.2 Internal Health (Detail)

```
GET /v1/internal/health
Authorization: Bearer <admin-token>
```

Erfordert Scope `admin:*`. Liefert immer `200`, auch wenn die IBKR-Session verloren ist - das Endpunkt-Ziel ist genau, das zu zeigen.

Response 200 (v0.4.0):

```json
{
  "auth_status": "ok",
  "cp_reachable": true,
  "last_tickle_at": "2026-04-25T11:30:14.512Z",
  "last_reauth_at": null,
  "session_age_s": 4823.42,
  "consecutive_reauth_failures": 0
}
```

| Feld | Bedeutung |
|---|---|
| `auth_status` | `ok` / `reauth_pending` / `auth_lost` / `cp_down` |
| `cp_reachable` | Letzter HTTP-Call zum CP-Gateway hat geantwortet |
| `last_tickle_at` | UTC-Timestamp des letzten Tickle-Calls (`null` vor dem ersten Aufruf) |
| `last_reauth_at` | UTC-Timestamp des letzten Reauth-Versuchs (`null` solange nie nötig) |
| `session_age_s` | Sekunden seit letztem Übergang in `auth_status=ok` |
| `consecutive_reauth_failures` | Aufeinanderfolgende fehlgeschlagene Reauth-Versuche; reset auf `0` bei `ok` |

#### Auth-Lifecycle

Der Service hält genau **eine** IBKR-Trading-Session offen und betreibt einen Hintergrund-Tickle-Job:

- Intervall: alle `BG_CP_TICKLE_INTERVAL_S` Sekunden (Default `60`).
- Bei `authenticated=false` aus `tickle`/`auth_status` wechselt der Status auf `reauth_pending`. Es werden bis zu drei `reauthenticate`-Versuche mit exponential backoff (Basis `2 s`) gemacht. Bleibt der Versuch erfolglos, fällt der Status auf `auth_lost`.
- HTTP-Fehler beim Aufruf des CP-Gateways setzen den Status auf `cp_down`.

Erfolgreiches Tickle nach Verlust setzt `auth_status` direkt zurück auf `ok`, ohne dass externe Endpunkte etwas tun müssen.

#### 503-Verhalten der Business-Endpunkte

Alle nachfolgenden Endpunkte (Quotes, Portfolio, Orders, Trades, Events) hängen die Dependency `require_session_ok` ein. Bei `auth_status in {auth_lost, cp_down}` antworten sie mit:

```
HTTP/1.1 503 Service Unavailable
Retry-After: 30
Content-Type: application/json

{"detail": "IBKR-Session nicht verfuegbar (status=auth_lost)"}
```

`/v1/health` und `/v1/internal/health` sind von dieser Sperre **ausgenommen**, damit Operations auch bei verlorener Session noch arbeiten können.

#### ENV-Variablen

| Variable | Default | Wirkung |
|---|---|---|
| `BG_CP_BASE_URL` | `http://cpgateway:5000` | Base-URL des internen CP Gateways |
| `BG_CP_TICKLE_INTERVAL_S` | `60` | Tickle-Periode in Sekunden |

---

## 4. Instruments

Alle Endpunkte erfordern Scope `instruments:read` und einen aktiven Auth-Lifecycle (`auth_status=ok`). Bei `auth_status in {auth_lost, cp_down}` antworten sie mit `503 Service Unavailable` + `Retry-After: 30` (Section 3.2).

Symbol-Lookups werden in einem **TTL-Cache (Default 7 Tage)** gehalten. conid-Mappings ändern sich praktisch nie, und CP-Gateway-Calls sind teuer und ratelimitiert (Section 10).

### 4.1 Search

```
GET /v1/instruments/search?symbol=AAPL[&exchange=NASDAQ]
Authorization: Bearer <token (instruments:read)>
```

Response 200 (v0.5.0 - Liste statt Paginierungs-Wrapper, da Suchen aktuell wenige Treffer liefern):

```json
[
  {
    "conid": 265598,
    "symbol": "AAPL",
    "company_name": "APPLE INC",
    "currency": "USD",
    "sec_type": "STK"
  }
]
```

| Feld | Bedeutung |
|---|---|
| `conid` | IBKR-interne Instrument-ID (int) |
| `symbol` | Ticker, immer upper-case |
| `company_name` | Klartext-Name (kann `null` sein) |
| `currency` | ISO-4217-Code (kann `null` sein) |
| `sec_type` | `STK` / `OPT` / `FUT` / ... (kann `null` sein) |

Unbekannte Symbole liefern `200` mit leerem Array, **nicht** `404`.

### 4.2 Detail

```
GET /v1/instruments/{conid}
Authorization: Bearer <token (instruments:read)>
```

Response 200 (v0.5.0):

```json
{
  "conid": 265598,
  "symbol": "AAPL",
  "company_name": "APPLE INC",
  "currency": "USD",
  "sec_type": "STK",
  "exchange": "NASDAQ"
}
```

---

## 5. Quotes

### 5.1 Snapshot

```
GET /v1/quotes/snapshot?conids=265598,14204&fields=last,bid,ask,volume,change_pct
Authorization: Bearer <token (quotes:read)>
```

`fields` ist eine Komma-Liste aus normalisierten Namen. Mapping zu IBKR-Field-IDs ist intern.

Response 200:

```json
{
  "items": [
    {
      "conid": 265598,
      "symbol": "AAPL",
      "availability": "delayed",
      "availability_raw": "DPB",
      "updated_at": "2026-04-24T17:30:00.123Z",
      "last":         { "value": "274.06", "currency": "USD" },
      "bid":          { "value": "274.05", "currency": "USD" },
      "ask":          { "value": "274.07", "currency": "USD" },
      "volume":       2080500000,
      "change_pct":   0.33
    },
    {
      "conid": 14204,
      "symbol": "SAP",
      "availability": "realtime",
      "availability_raw": "RPB",
      "updated_at": "2026-04-24T17:30:00.480Z",
      "last":         { "value": "140.96", "currency": "EUR" },
      "bid":          { "value": "140.56", "currency": "EUR" },
      "ask":          { "value": "140.96", "currency": "EUR" },
      "volume":       7430000,
      "change_pct":   -5.91
    }
  ]
}
```

**Server-internals:** Snapshot-Endpoint primt selbst (zwei IBKR-Calls), Consumer sieht immer Daten oder Error.

### 5.2 Stream (SSE)

```
GET /v1/quotes/stream?conids=265598,14204&fields=last,bid,ask
Authorization: Bearer <token (quotes:read)>
Accept: text/event-stream
```

Response: `text/event-stream` mit Frames pro Update:

```
event: quote
id: 1776972009330-265598
data: {"conid":265598,"symbol":"AAPL","last":{"value":"274.06","currency":"USD"},"updated_at":"2026-04-24T17:30:00.123Z"}

event: quote
id: 1776972011022-14204
data: {"conid":14204,"symbol":"SAP","bid":{"value":"140.56","currency":"EUR"},"updated_at":"2026-04-24T17:30:01.022Z"}

event: keepalive
data: {}
```

- **Reconnect:** Client sendet `Last-Event-ID` Header, Server resumes.
- **Subscription-Lifecycle:** Subscription bleibt aktiv solange die HTTP-Verbindung steht. Disconnect → Server dekrementiert Refcount, ggf. unsubscribe bei IBKR.

---

## 6. Portfolio

### 6.1 Summary

```
GET /v1/portfolio/{accountId}
Authorization: Bearer <token (portfolio:read)>
```

Response 200:

```json
{
  "account_id": "U25235077",
  "currency": "EUR",
  "net_liquidation":  { "value": "9724.29", "currency": "EUR" },
  "available_funds":  { "value": "179.54",  "currency": "EUR" },
  "buying_power":     { "value": "179.54",  "currency": "EUR" },
  "gross_position_value": { "value": "9544.75", "currency": "EUR" },
  "leverage": 0.98,
  "as_of": "2026-04-24T17:30:00Z"
}
```

### 6.2 Positions

```
GET /v1/portfolio/{accountId}/positions
Authorization: Bearer <token (portfolio:read)>
```

Response 200:

```json
{
  "items": [
    {
      "conid": 265598,
      "symbol": "AAPL",
      "sec_type": "STK",
      "quantity": "10.5",
      "avg_cost":     { "value": "245.30", "currency": "USD" },
      "market_value": { "value": "2877.63", "currency": "USD" },
      "unrealized_pnl": { "value": "299.13", "currency": "USD" },
      "as_of": "2026-04-24T17:30:00Z"
    }
  ]
}
```

### 6.3 Ledger

```
GET /v1/portfolio/{accountId}/ledger
Authorization: Bearer <token (portfolio:read)>
```

Response 200:

```json
{
  "items": [
    {
      "currency": "USD",
      "cash_balance": { "value": "-150.30", "currency": "USD" },
      "settled_cash": { "value": "-150.30", "currency": "USD" },
      "interest_accrued": { "value": "0.42", "currency": "USD" }
    },
    {
      "currency": "EUR",
      "cash_balance": { "value": "329.84", "currency": "EUR" },
      "settled_cash": { "value": "329.84", "currency": "EUR" },
      "interest_accrued": { "value": "0.00", "currency": "EUR" }
    }
  ]
}
```

---

## 7. Orders

### 7.1 Place

```
POST /v1/orders
Authorization: Bearer <token (orders:write)>
Idempotency-Key: 7c3b2a8e-...
Content-Type: application/json

{
  "account_id": "U25235077",
  "conid": 265598,
  "side": "BUY",
  "quantity": 1,
  "order_type": "LMT",
  "limit_price": "270.00",
  "tif": "DAY"
}
```

Response 202:

```json
{
  "order_id": "ord_abc123",
  "status": "submitted",
  "broker_order_id": "1979132751",
  "preview": {
    "estimated_commission": { "value": "1.00", "currency": "USD" },
    "warnings": ["mandatory_cap_price"]
  },
  "submitted_at": "2026-04-24T17:30:00Z"
}
```

### 7.2 Get Status

```
GET /v1/orders/{order_id}
Authorization: Bearer <token (orders:read)>
```

Response 200:

```json
{
  "order_id": "ord_abc123",
  "status": "filled",
  "filled_quantity": 1,
  "avg_fill_price": { "value": "270.00", "currency": "USD" },
  "commission": { "value": "1.00", "currency": "USD" },
  "submitted_at": "2026-04-24T17:30:00Z",
  "filled_at": "2026-04-24T17:31:42Z"
}
```

### 7.3 Cancel

```
DELETE /v1/orders/{order_id}
Authorization: Bearer <token (orders:write)>
Idempotency-Key: 7c3b2a8e-...
```

Response 200:

```json
{
  "order_id": "ord_abc123",
  "status": "cancelled",
  "cancelled_at": "2026-04-24T17:31:00Z"
}
```

### 7.4 What-If (Preview)

Vor einer echten Order kann man eine Risk-Preview holen:

```
POST /v1/orders/whatif
Authorization: Bearer <token (orders:read)>
Content-Type: application/json

{
  "account_id": "U25235077",
  "conid": 265598,
  "side": "BUY",
  "quantity": 1,
  "order_type": "LMT",
  "limit_price": "270.00",
  "tif": "DAY"
}
```

Response 200:

```json
{
  "estimated_amount":      { "value": "270.00", "currency": "USD" },
  "estimated_commission":  { "value": "1.00",   "currency": "USD" },
  "estimated_total":       { "value": "271.00", "currency": "USD" },
  "margin_impact": {
    "current_funds":    { "value": "179.54", "currency": "EUR" },
    "after_funds":      { "value": "ca. 91.00", "currency": "EUR" }
  },
  "warnings": [
    {
      "code": "no_market_data_subscription",
      "raw_id": 21,
      "message": "Order would be submitted without realtime market data."
    }
  ]
}
```

---

## 8. Trades

```
GET /v1/trades?from=2026-04-01&to=2026-04-24&limit=200
Authorization: Bearer <token (trades:read)>
```

Response 200:

```json
{
  "items": [
    {
      "execution_id": "00012978.69e8f717.01.01",
      "order_id": "ord_abc123",
      "broker_execution_id": "00012978.69e8f717.01.01",
      "conid": 265598,
      "symbol": "AAPL",
      "side": "BUY",
      "quantity": "0.9688",
      "price":      { "value": "140.72", "currency": "USD" },
      "net_amount": { "value": "136.33", "currency": "USD" },
      "commission": { "value": "0.00", "currency": "USD" },
      "executed_at": "2026-04-22T14:05:52Z"
    }
  ],
  "summary": {
    "trade_count": 70,
    "commissions_total_by_currency": [
      { "currency": "USD", "value": "24.75" }
    ]
  },
  "next_cursor": null
}
```

---

## 9. Events Stream (SSE)

```
GET /v1/events/stream
Authorization: Bearer <token (events:read)>
Accept: text/event-stream
```

Server pusht Events:

```
event: execution_report
id: 1776974321-abc
data: {"order_id":"ord_abc123","status":"partial_fill","filled_quantity":0.5,...}

event: position_update
id: 1776974345-xyz
data: {"conid":265598,"new_quantity":"11.0","change":"0.5"}

event: order_status_change
id: 1776974400-def
data: {"order_id":"ord_def456","old_status":"submitted","new_status":"cancelled","reason":"user_cancel"}

event: keepalive
data: {}
```

---

## 10. Rate-Limits

Per Token (Default-Werte, konfigurierbar):

| Endpoint-Klasse | Rate (req/min) | Burst |
|---|---|---|
| Reads (instruments, quotes, portfolio, trades) | 600 | 60 |
| Snapshot-Bursts | 120 | 30 |
| Stream-Subscribes | 60 | 10 |
| Orders (write) | 60 | 5 |
| Auth | 10 | 3 |

Bei Überschreitung: `429` mit `Retry-After`-Header.

---

## 11. Internals (nicht öffentlich)

Nur dokumentiert für Service-Entwickler, nicht für Consumer:

- **Subscription-Refcount-Map:** `{conid → {fields-bitmask → ref_count}}`
- **Order-Cache:** `{order_id → order_state}` mit TTL 24 h für Idempotency-Antworten
- **Auth-Session-Tickle:** Hintergrund-Job alle 60 s `POST /tickle` an CP-Gateway
- **Field-Mapping-Tabelle:** normalisierte Quote-Field-Namen ↔ IBKR-Field-IDs (z.B. `last` ↔ `31`, `bid` ↔ `84`)
- **Pacing-Throttle:** Token-Bucket pro IBKR-Endpoint-Klasse mit Backpressure auf eigene Queue
- **Reconnect-Logic:** bei Session-Drop: 3 Versuche `reauthenticate`, dann `503` mit `Retry-After: 60` für Consumer + Health-Endpoint flagt `auth_session_expired`

---

## 12. OpenAPI / Schema

Wird in einer der ersten Karten als `openapi.yaml` neben dieser Doku gepflegt. Diese Markdown-Datei bleibt die Referenz für Konzepte und Erklärungen.

---

*Working Draft — ergänzt sich mit jeder AP-01-Karte.*
