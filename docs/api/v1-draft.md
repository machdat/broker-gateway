# broker-gateway API v1 — Working Draft

**Status:** Draft. Wird im Rahmen von AP-01 (KanPrompt-Projekt `broker-gateway`) iterativ konsolidiert. Jede Implementierungs-Karte aktualisiert die zugehörigen Abschnitte.
**Stand:** 2026-04-25 (Service-Version 0.9.0)

> ⚠ **Hinweis für Consumer:** Bis v1.0.0 freigegeben ist, ist diese Spezifikation nicht stabil. Consumer-Implementierungen sollten erst nach formaler v1.0.0-Markierung beginnen.

### Implementation Status (Service-Version 0.9.0)

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
| 5.1 Quotes-Snapshot (`GET /v1/quotes/snapshot`) | ✅ in v0.6.0 (vereinfachter Body, siehe Section 5.1) |
| Availability-Normalisierung 6509 → realtime/delayed/frozen | ✅ in v0.6.0 |
| 5.2 Quotes-Stream (`GET /v1/quotes/stream`) | ✅ in v0.7.0 (Refcount + Fan-Out + 5-s-Cool-Down) |
| 6.1 Portfolio-Summary (`GET /v1/portfolio/{accountId}`) | ✅ in v0.8.0 (Aggregat aus Positions + Ledger) |
| 6.2 Positions (`GET /v1/portfolio/{accountId}/positions`) | ✅ in v0.8.0 |
| 6.3 Ledger (`GET /v1/portfolio/{accountId}/ledger`) | ✅ in v0.8.0 |
| Money-Normalisierung (`{value, currency}` als Single Source of Truth) | ✅ in v0.8.0 |
| 7.1 Place (`POST /v1/orders`) mit Idempotency-Key + Reply-Confirmation-Loop | ✅ in v0.9.0 (vereinfachter Body, siehe Section 7.1) |
| 7.2 Status (`GET /v1/orders/{order_id}`) mit Money-Normalisierung | ✅ in v0.9.0 |
| 7.3 Cancel (`DELETE /v1/orders/{order_id}`) mit Idempotency-Key | ✅ in v0.9.0 |
| Idempotency-Cache (`Idempotency-Key` -> 200-Replay) | ✅ in v0.9.0 |
| 7.4 What-If (Preview) | ⏳ Folgekarte |
| 1.6 Error-Modell (Schema mit `error.code`/`error.message`) | ⏳ aktuell FastAPI-Default `{"detail": "..."}` |
| Alle anderen | ⏳ Folgekarten in AP-01 |

Die Beispiel-Response in Section 3.1 zeigt die geplante v1.0-Form. In der aktuell ausgelieferten v0.9.0 ist `version` die Service-Version `0.9.0` (Single Source of Truth: `pyproject.toml` + `broker_gateway.__version__`).

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
GET /v1/quotes/snapshot?conids=265598,272093[&fields=last,bid,ask,availability]
Authorization: Bearer <token (quotes:read)>
```

`fields` ist eine Komma-Liste aus normalisierten Namen. Mapping zu IBKR-Field-IDs ist intern (Single Source of Truth: `broker_gateway.cp.quotes.FIELD_ALIASES`).

| Alias | IBKR-Code | Bedeutung |
|---|---|---|
| `last` | `31` | Letzter gehandelter Preis |
| `bid` | `84` | Aktuelles Bid |
| `ask` | `86` | Aktuelles Ask |
| `volume` | `7762` | Tagesvolumen |
| `change_pct` | `83` | Änderung in % |
| `high` | `70` | Tageshoch |
| `low` | `71` | Tagestief |
| `availability` | `6509` | Real-/Delayed-/Frozen-Code (intern automatisch normalisiert) |

Default-Felder (wenn `fields` weggelassen): `last,bid,ask,availability`. `availability` (6509) wird **immer** mit angefordert, auch wenn es nicht in `fields` steht - Consumer brauchen die Information, ob sie Realtime- oder Delayed-Daten lesen.

**Constraint:** Maximal 5 conids pro Snapshot-Request (CP-Gateway-Limit). Bei mehr → `422 Unprocessable Content`.

Response 200 (v0.6.0 - Liste statt Wrapper, vereinfachte Felder):

```json
[
  {
    "conid": 265598,
    "last": "150.50",
    "bid": "150.45",
    "ask": "150.55",
    "volume": null,
    "change_pct": null,
    "high": null,
    "low": null,
    "availability": "delayed",
    "availability_raw": "DPB",
    "updated_at": "2026-04-25T11:30:14.481Z"
  }
]
```

| Feld | Bedeutung |
|---|---|
| `last`/`bid`/`ask`/... | als String, um Float-Rounding zu vermeiden; `null` falls IBKR den Wert (noch) nicht geliefert hat |
| `availability` | normalisiert auf `realtime` / `delayed` / `frozen` / `null` (Single Source of Truth: `broker_gateway.availability.map_availability`) |
| `availability_raw` | Originaler IBKR-6509-Code (z.B. `DPB`) - bleibt erhalten, falls Consumer ihn ausnahmsweise braucht |
| `updated_at` | Aus `_updated`-Millisekunden-Timestamp des CP-Gateways, in UTC |

#### First-Call-Prime (intern)

CP-Gateway-Quirk (Anhang 1.5): der erste Snapshot-Call für eine neue conid liefert leere Felder - das `marketdata/snapshot`-Endpunkt primt CP-intern erst die Subscription. Erst der zweite Call hat die echten Werte.

Der Service absorbiert das nach aussen: jeder `/v1/quotes/snapshot`-Aufruf macht intern **zwei** sequenzielle CP-Gateway-Calls (kurzer 300 ms-Delay dazwischen) und gibt nur das Second-Call-Ergebnis zurück. Consumer sieht immer Daten oder Error.

### 5.2 Stream (SSE)

```
GET /v1/quotes/stream?conids=265598,272093[&fields=last,bid,ask]
Authorization: Bearer <token (quotes:read)>
Accept: text/event-stream
[Last-Event-ID: <last-seen-event-id>]
```

`fields` und Default-Verhalten identisch zu Section 5.1 (Snapshot). `availability` wird intern immer angefordert.

Response `200 OK` mit `Content-Type: text/event-stream` und Frames im SSE-Format:

```
id: 0
event: quote
data: {"conid":265598,"last":"150.50","bid":"150.45","ask":"150.55","availability":"delayed","availability_raw":"DPB","updated_at":"2026-04-25T11:30:14.481Z"}

id: 1
event: quote
data: {"conid":272093,"last":"320.10","bid":"320.05","ask":"320.15","availability":"delayed","availability_raw":"DPB","updated_at":"2026-04-25T11:30:14.498Z"}
```

`event` ist immer `quote`, `data` enthält dasselbe Schema wie ein Snapshot-Eintrag. `id` ist eine vom Server vergebene, **monoton steigende** Event-ID (Single Source of Truth: `broker_gateway.streams.manager._ConidSubscription._next_event_id`).

#### Subscription-Refcount + Fan-Out

Anders als das CP-Gateway, das Subscriptions session-global hält, bietet `broker-gateway` Refcount + Fan-Out:

- Pro `conid` läuft genau **eine** interne Subscription (Polling gegen `/iserver/marketdata/snapshot`, kein nativer CP-Stream-Endpunkt in v1).
- Mehrere parallele HTTP-Consumer auf denselben `conid` teilen sich diese eine Subscription. Jeder Consumer bekommt jede Quote-Aktualisierung.
- Disconnect eines Consumers (TCP-Close, Browser-Reload, Process-Exit) dekrementiert den Refcount. Bei `refcount=0` startet ein **5-Sekunden-Cool-Down**; verbindet binnen dieser Zeit ein neuer Consumer, wird der Cool-Down gecancelt und die Subscription bleibt warm. Andernfalls wird `/iserver/marketdata/{conid}/unsubscribe` gerufen.

#### Reconnect via Last-Event-ID

Sendet der Client beim Reconnect den `Last-Event-ID`-Header, liefert der Server zunächst alle gepufferten Events mit `id > Last-Event-ID` und schaltet dann auf den Live-Stream um. Buffergröße ist 200 Events pro `conid`. Wenn der Reconnect zu spät kommt, fehlen Events - Consumer sollte in dem Fall einen `/v1/quotes/snapshot` als Cold-Start ergänzen.

#### Limits + Fehlercodes

| Status | Bedingung |
|---|---|
| `429 Too Many Requests` mit `Retry-After: 30` | Manager hat das CP-Gateway-Limit von **250 parallel aktiven conids** erreicht |
| `503 Service Unavailable` mit `Retry-After: 30` | `auth_status in {auth_lost, cp_down}` (siehe Section 3.2) |
| `403 Forbidden` | Token hat nicht `quotes:read` |
| `401 Unauthorized` | Token fehlt / ungültig / abgelaufen |
| `422 Unprocessable Content` | leere/ungültige `conids` oder unbekannte `fields` |

#### Verhältnis Snapshot ↔ Stream

Beide Endpunkte nutzen dasselbe Field-Alias-Mapping und denselben Quote-Body. Wer einmalig Daten will, ruft Snapshot. Wer kontinuierlich Updates braucht, hängt sich an Stream. Mischbetrieb (Snapshot warm-start + Stream danach) wird vom Server gut vertragen, da Subscription-Refcount global ist.

---

## 6. Portfolio

Alle Endpunkte erfordern Scope `portfolio:read` und einen aktiven Auth-Lifecycle (`auth_status=ok`). Bei `auth_status in {auth_lost, cp_down}` antworten sie mit `503 Service Unavailable` + `Retry-After: 30` (Section 3.2).

Antworten werden in einem **TTL-Cache (Default 30 s, ENV `BG_PORTFOLIO_TTL_S`)** gehalten. Das Order-Subsystem (Section 7) ruft `PortfolioService.invalidate(account_id)` nach erfolgreichem Place/Cancel auf, damit nachfolgende Reads einen frischen Stand liefern.

Geldwerte folgen Section 1.9 (`{value, currency}` mit String-Value gegen Float-Drift). Single Source of Truth fuer die Normalisierung: `broker_gateway.money.normalize_money`.

### 6.1 Summary

```
GET /v1/portfolio/{accountId}
Authorization: Bearer <token (portfolio:read)>
```

Aggregat ueber Positions + Ledger. `base_currency` ist die erste Ledger-Currency (typischerweise die Konto-Heimatwaehrung). `cash_total`, `positions_value` und `net_liquidation` werden ausschliesslich in `base_currency` aggregiert; Werte in anderen Waehrungen finden sich in Section 6.2/6.3.

Response 200 (v0.8.0):

```json
{
  "account_id": "U25235077",
  "base_currency": "USD",
  "cash_total":       { "value": "25000", "currency": "USD" },
  "positions_value":  { "value": "3105.5", "currency": "USD" },
  "net_liquidation":  { "value": "28105.5", "currency": "USD" },
  "position_count": 2
}
```

### 6.2 Positions

```
GET /v1/portfolio/{accountId}/positions
Authorization: Bearer <token (portfolio:read)>
```

Response 200 (v0.8.0 - nackte Liste analog Sections 4.1/5.1):

```json
[
  {
    "account_id": "U25235077",
    "conid": 265598,
    "quantity": "10",
    "avg_cost":     { "value": "145.0", "currency": "USD" },
    "market_price": { "value": "150.5", "currency": "USD" },
    "market_value": { "value": "1505.0", "currency": "USD" }
  }
]
```

`quantity` ist Decimal-String (Bruchstuecke moeglich). `market_value` wird vom CP-Gateway geliefert; fehlt es, leitet der Service es aus `quantity * market_price` ab. Schlaegt das fehl (z.B. fehlende Market-Daten), bleibt das Feld `null`.

### 6.3 Ledger

```
GET /v1/portfolio/{accountId}/ledger
Authorization: Bearer <token (portfolio:read)>
```

Response 200 (v0.8.0):

```json
{
  "account_id": "U25235077",
  "entries": [
    {
      "currency": "USD",
      "cash_balance": { "value": "25000.0", "currency": "USD" },
      "settled_cash": { "value": "25000.0", "currency": "USD" }
    },
    {
      "currency": "EUR",
      "cash_balance": { "value": "5000.0", "currency": "EUR" },
      "settled_cash": { "value": "5000.0", "currency": "EUR" }
    }
  ]
}
```

#### Cache-Invalidierung

| Trigger | Wirkung |
|---|---|
| `BG_PORTFOLIO_TTL_S` Sekunden seit letztem Refresh | naechster Read schlaegt das CP-Gateway erneut |
| `PortfolioService.invalidate(account_id)` (Order-Lifecycle, Karte 09) | summary/positions/ledger sofort weg, naechster Read holt frisch |

#### ENV

| Variable | Default | Wirkung |
|---|---|---|
| `BG_PORTFOLIO_TTL_S` | `30` | TTL des Portfolio-Caches in Sekunden |

---

## 7. Orders

### 7.0 Idempotency-Konvention

Schreiboperationen (`POST /v1/orders`, `DELETE /v1/orders/{order_id}`) verlangen den HTTP-Header `Idempotency-Key`. Konvention: vom Caller erzeugte UUID v4 oder andere kollisionsarme Token. Pflicht-Verhalten:

- **Fehlt** der Header: `400 Bad Request`, Body `{"detail": "Idempotency-Key-Header ist Pflicht"}`.
- **Erstaufruf** mit Key wird verarbeitet und das Ergebnis im Idempotency-Cache abgelegt (TTL Default 24 h, ENV `BG_IDEMPOTENCY_TTL_S`).
- **Replay** mit demselben Key liefert die gespeicherte Antwort, aber mit Status `200 OK` (statt 201 für Place). Der Body ist identisch zum Erstaufruf - der CP-Gateway wird nicht erneut getroffen.
- Der Schluessel wird pro HTTP-Methode gescoped, d.h. POST und DELETE koennen denselben Key tragen, ohne zu kollidieren.

| ENV | Default | Wirkung |
|---|---|---|
| `BG_IDEMPOTENCY_TTL_S` | `86400` | TTL in Sekunden fuer einen Idempotency-Eintrag |

### 7.1 Place (implementiert in v0.9.0)

```
POST /v1/orders
Authorization: Bearer <token (orders:write)>
Idempotency-Key: 7c3b2a8e-...
Content-Type: application/json

{
  "account_id": "U25235077",
  "conid": 265598,
  "side": "BUY",
  "quantity": "1",
  "order_type": "LMT",
  "limit_price": "270.00",
  "tif": "DAY"
}
```

`order_type` ist eines aus `LMT`, `MKT`, `STP`, `STP-LMT` — Werte ausserhalb dieser Menge werden mit `422 Unprocessable Content` abgelehnt. `quantity`, `limit_price`, `stop_price` sind Decimal-Strings (kein Float). Combinations:

| `order_type` | Pflichtfelder |
|---|---|
| `LMT` | `limit_price` |
| `MKT` | (keine) |
| `STP` | `stop_price` |
| `STP-LMT` | `limit_price` + `stop_price` |

Reply-Confirmation-Loop: Das CP-Gateway antwortet bei einer Place-Anfrage gelegentlich mit einer Liste von Warning-Confirmations (Schema `[{id, message: [...]}]`) statt direkt mit der Order. Der Service quittiert solche Warnings transparent per `POST /iserver/reply/{id}` mit `{"confirmed": true}` und liefert erst die finale Order. Die Warning-Texte werden an den Caller im Feld `warnings` durchgereicht.

Response 201 (Erstaufruf):

```json
{
  "order_id": "1000000",
  "account_id": "U25235077",
  "conid": 265598,
  "side": "BUY",
  "quantity": "1",
  "order_type": "LMT",
  "tif": "DAY",
  "status": "PendingSubmit",
  "limit_price": "270.00",
  "stop_price": null,
  "avg_fill_price": null,
  "commission": null,
  "filled_quantity": null,
  "submitted_at": null,
  "warnings": []
}
```

Response 200 bei Replay (gleicher Key): identischer Body.

Fehler:

| Status | Bedingung |
|---|---|
| 400 | `Idempotency-Key`-Header fehlt |
| 401/403 | kein Token / falscher Scope |
| 422 | `order_type` ungueltig oder Pflichtfeld fehlt |
| 503 | IBKR-Session nicht verfuegbar (`Retry-After: 30`) |

### 7.2 Status (implementiert in v0.9.0)

```
GET /v1/orders/{order_id}
Authorization: Bearer <token (orders:write)>
```

Response 200:

```json
{
  "order_id": "1000000",
  "account_id": "U25235077",
  "conid": 265598,
  "side": "BUY",
  "quantity": "1",
  "order_type": "LMT",
  "tif": "DAY",
  "status": "Filled",
  "limit_price": "150.00",
  "stop_price": null,
  "avg_fill_price": { "value": "150.00", "currency": "USD" },
  "commission":     { "value": "1.00",   "currency": "USD" },
  "filled_quantity": "1",
  "submitted_at": null,
  "warnings": []
}
```

`status`-Vokabular spiegelt den CP-Gateway-Lifecycle: `PendingSubmit`, `Submitted`, `Filled`, `Cancelled`, `Rejected`, `Inactive`. Geldfelder folgen Section 1.9.

### 7.3 Cancel (implementiert in v0.9.0)

```
DELETE /v1/orders/{order_id}
Authorization: Bearer <token (orders:write)>
Idempotency-Key: 7c3b2a8e-...
X-Account-Id: U25235077
```

Da das CP-Gateway den Account-Kontext braucht, ist `X-Account-Id` Pflicht. Fehlt der Header, antwortet der Service mit 400.

Response 200:

```json
{
  "order_id": "1000000",
  "status": "Cancelled",
  "cancelled_at": "2026-04-25T07:30:00Z"
}
```

### Cache-Invalidation (Portfolio)

`POST /v1/orders` und `DELETE /v1/orders/{order_id}` busten beim Erstaufruf den `PortfolioService`-Cache fuer das betroffene Account, damit ein anschliessender `GET /v1/portfolio/{accountId}/positions`-Aufruf den frischen Bestand liefert.

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
