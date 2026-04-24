# broker-gateway API v1 — Working Draft

**Status:** Draft. Wird im Rahmen von AP-01 (KanPrompt-Projekt `broker-gateway`) iterativ konsolidiert. Jede Implementierungs-Karte aktualisiert die zugehörigen Abschnitte.
**Stand:** 2026-04-25

> ⚠ **Hinweis für Consumer:** Bis v1.0.0 freigegeben ist, ist diese Spezifikation nicht stabil. Consumer-Implementierungen sollten erst nach formaler v1.0.0-Markierung beginnen.

### Implementation Status (Service-Version 0.1.0)

| Section | Implementiert |
|---|---|
| 3.1 Public Health (`GET /v1/health`) | ✅ in v0.1.0 |
| Alle anderen | ⏳ Folgekarten in AP-01 |

Die Beispiel-Response in Section 3.1 zeigt die geplante v1.0-Form. In der aktuell ausgelieferten v0.1.0 ist `version` der Service-Version `0.1.0` (Single Source of Truth: `pyproject.toml` + `broker_gateway.__version__`).

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

| Scope | Berechtigt zu |
|---|---|
| `instruments:read` | Symbol-Lookup, conid-Lookup |
| `quotes:read` | Snapshots + Streams |
| `portfolio:read` | Portfolio + Positions + Ledger |
| `orders:read` | Order-Status abfragen |
| `orders:write` | Orders platzieren / canceln |
| `events:read` | Events-Stream (Execution-Reports etc.) |
| `trades:read` | Historische Trades |
| `admin:*` | Token-Verwaltung, Service-internals |

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

### 2.1 Token erstellen

**Hinweis:** Token-Erstellung selbst ist nicht öffentlich. In v1 läuft sie via Admin-CLI / Config-Datei (provisioniert mit dem Service-Deployment). Dieser Endpoint ist nur für Token-Refresh und Service-Inspektion.

```
POST /v1/auth/token
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "psm-production",
  "scopes": ["quotes:read", "portfolio:read", "instruments:read"],
  "ttl_days": 365
}
```

Response 200:

```json
{
  "token": "bgw_live_a3f7...",
  "name": "psm-production",
  "scopes": ["quotes:read", "portfolio:read", "instruments:read"],
  "expires_at": "2027-04-24T17:30:00Z",
  "created_at": "2026-04-24T17:30:00Z"
}
```

### 2.2 Token revoken

```
DELETE /v1/auth/token/{token-id}
Authorization: Bearer <admin-token>
```

Response 204.

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
  "version": "1.0.0"
}
```

### 3.2 Internal Health (Detail)

```
GET /v1/internal/health
Authorization: Bearer <admin-token>
```

Response 200:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "broker": {
    "name": "ibkr-cp-gateway",
    "version": "10.44.1h",
    "session": {
      "authenticated": true,
      "established": true,
      "connected": true,
      "session_age_seconds": 4823,
      "last_tickle": "2026-04-24T17:29:14Z"
    }
  },
  "subscriptions": {
    "active_count": 17,
    "max": 250
  },
  "queue": {
    "depth": 0,
    "throttle_per_sec": 50
  }
}
```

Bei `503 Service Unavailable` — Body enthält `error.code` aus `{auth_session_expired, broker_unavailable, queue_overflow}` und `retry_after`.

---

## 4. Instruments

### 4.1 Search

```
GET /v1/instruments/search?symbol=AAPL&sec_type=STK&limit=20
Authorization: Bearer <token (instruments:read)>
```

Response 200:

```json
{
  "items": [
    {
      "conid": 265598,
      "symbol": "AAPL",
      "name": "APPLE INC",
      "sec_type": "STK",
      "exchange": "NASDAQ",
      "primary_exchange": "NASDAQ",
      "currency": "USD"
    }
  ],
  "next_cursor": null
}
```

### 4.2 Detail

```
GET /v1/instruments/{conid}
Authorization: Bearer <token (instruments:read)>
```

Response 200:

```json
{
  "conid": 265598,
  "symbol": "AAPL",
  "name": "APPLE INC",
  "sec_type": "STK",
  "exchange": "NASDAQ",
  "primary_exchange": "NASDAQ",
  "currency": "USD",
  "min_tick": "0.01",
  "valid_exchanges": ["NASDAQ", "BATS", "ARCA", "IEX"]
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
