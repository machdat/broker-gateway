# 05 — API (kuratierter Einstieg für Konsumenten)

Konsumenten-orientierter Einstieg in die `broker-gateway`-API. Erklärt
das Verhältnis zwischen formaler Spec und der **Spiegel-Doku**, die
jeder Consumer in seinem Repo zu führen hat.

> **SSOT-Prinzip:** Die formale Wahrheit lebt in [`docs/api/v1.md`](api/v1.md).
> Dieses Dokument hier ist **kuratierter Einstieg** und konventioniert das
> Konsumenten-Modell — es **dupliziert keine Schemas**. Architektur-Hintergrund
> liegt in [`docs/02-architecture.md`](02-architecture.md), Sicherheits-
> Aussagen in [`docs/04-security.md`](04-security.md).
> **Begriffsklärungen:** [`docs/06-glossary.md`](06-glossary.md).

**Stand:** v1.11.0, 2026-04-30.

## Inhalt

1. [Zweck und Abgrenzung](#1-zweck-und-abgrenzung)
2. [SSOT-Prinzip](#2-ssot-prinzip)
3. [Konventionen quer ueber alle Endpunkte](#3-konventionen-quer-ueber-alle-endpunkte)
4. [Endpunkt-Landkarte](#4-endpunkt-landkarte)
5. [Stream-Patterns (SSE)](#5-stream-patterns-sse)
6. [Versionsstrategie aus Consumer-Sicht](#6-versionsstrategie-aus-consumer-sicht)
7. [Consumer-Gegenpart-Konvention](#7-consumer-gegenpart-konvention)
8. [Verweise und offene Fragen](#8-verweise-und-offene-fragen)

---

## 1. Zweck und Abgrenzung

| Ebene | Datei | Wofür |
|---|---|---|
| **Formale Spec** | [`docs/api/v1.md`](api/v1.md) | Endpunkte, Request/Response-Schemata, Error-Modell (Section 1.6), Beispiele. **Hier sucht ein Consumer das genaue Format.** |
| **Kuratierter Einstieg** | dieses Dokument | Erklärt Konventionen, Stream-Patterns, Versionsumgang und die Spiegel-Doku-Konvention. **Hier sucht ein Consumer den richtigen Einstieg.** |
| **Architektur** | [`docs/02-architecture.md`](02-architecture.md) | Warum so gebaut: Singular-Halter, Stateless-Außen, Logging, Test-Strategie. |
| **Security** | [`docs/04-security.md`](04-security.md) | Token-Modell, Scopes, Header-Redaktion, Vorfall-Reaktion. |

**Aus dem Scope dieses Dokuments:** keine Endpunkt-Schemata, keine
Status-Code-Tabellen, keine Body-Beispiele in voller Tiefe — die leben
in `v1.md`. Wo ein Consumer ein konkretes Feld nachschlagen will, lautet
der Verweis: „siehe `docs/api/v1.md` Section X.Y".

## 2. SSOT-Prinzip

`docs/api/v1.md` ist die **alleinige formale Wahrheit** des
v1-Vertrags. Konsequenzen:

- Schemas, Endpunkte, Status-Codes und Error-Codes werden **nur dort**
  gepflegt.
- **Keine Kopien** in anderen Dokumenten (auch nicht in dieser Datei).
- Jede Änderung an `v1.md` triggert eine Review der Consumer-Spiegel-
  Doks (siehe Sektion 7) — Verantwortung des jeweiligen Consumers.
- `02-architecture.md` und `05-api.md` verweisen ausschließlich auf
  Sektionen in `v1.md`, statt Schemas zu wiederholen.

Jede Diskrepanz zwischen `v1.md` und tatsächlicher Implementierung ist
ein Bug — Reparatur via separater Karte (z.B. AP-03 Drift-Detection
oder eine eigene „API-Spec-Aktualisierung gegen aktuellen Stand").
Diese Datei hier listet aufgefallene Diskrepanzen unter Sektion 8.

## 3. Konventionen quer über alle Endpunkte

Knappe Konsumenten-Sicht. Vertiefung in `v1.md` Section 1.

### 3.1 Auth-Header

```
Authorization: Bearer <opaque-token>
```

- Format und Lifecycle: [`docs/04-security.md`](04-security.md) Sektion 2.
- Scope-Matrix: [`docs/04-security.md`](04-security.md) Sektion 3 +
  `v1.md` Section 1.4.
- Token-Werte werden **niemals** in Logs/Recordings geschrieben (siehe
  `04-security.md` Sektion 4-5).

### 3.2 Idempotency-Key

Pflicht-Header für Schreib-Endpunkte (`POST /v1/orders`,
`DELETE /v1/orders/{id}`):

```
Idempotency-Key: <client-generated UUID, z.B. UUIDv4>
```

- Server speichert `key -> response` für Default 24 h
  (`BG_IDEMPOTENCY_TTL_S`).
- Wiederholungen mit demselben Key liefern identische Response,
  ohne erneuten Broker-Call.
- Beim Service-Restart ist der Cache leer (transient — `04-security.md`
  Sektion 9). Konsequenz für Consumer: pro Logik-Schritt einen
  **frischen** Key generieren, sodass Replay ein bewusster Akt ist.

### 3.3 Error-Envelope

Alle Fehler ab v1.5.0 folgen demselben Schema (`v1.md` Section 1.6):

```json
{
  "error": {
    "code": "missing_scope",
    "message": "...",
    "request_id": "<corr-id>",
    "retry_after_s": 30,
    "extra": {"required_scope": "orders:write"}
  }
}
```

Konsumenten lesen `error.code` (maschinenlesbar, stabil) und
`error.message` (menschlesbar, kann sich ändern). Der `request_id`
korreliert mit den Inbound-Logs des Services für Support-Anfragen.

### 3.4 Rate-Limit-Verhalten

- Service serialisiert pro Endpoint-Klasse über interne Token-Buckets
  (`broker_gateway_pacing_violations_total`-Metric, siehe `02-architecture.md`
  Sektion 8.6).
- Bei IBKR-Pacing-Violation reagiert der Service mit `429`
  (Re-Queue intern) oder `503` mit `Retry-After`.
- Consumer **muss** `Retry-After` respektieren — sonst eskaliert die
  Drosselung.

### 3.5 Streaming via SSE

Quote- und Event-Streams sind **SSE** (`text/event-stream`). Verhalten
und Reconnect-Logik in Sektion 5.

### 3.6 Versions-Header

- `/v1` bleibt rückwärtskompatibel.
- Bei geplanter Deprecation einer Major-Version setzt der Service:
  - `Deprecation: true`
  - `Sunset: <RFC7231-Datum>`

### 3.7 Retry-Semantik

| Status | Wiederholbar? |
|---|---|
| `2xx` | n/a |
| `3xx` | n/a (heute keine Redirects) |
| `400`, `403`, `404`, `409`, `422` | **nein**, Consumer-Fehler |
| `401` | **nein**, Token rotieren / re-authenticaten |
| `429` | **ja**, mit Backoff (`Retry-After` beachten) |
| `5xx` | **ja**, mit Backoff (`Retry-After` beachten); bei `503` + `Retry-After: 30` ist `auth_lost` der typische Trigger |

Schreib-Endpunkte: Retry **nur** mit demselben `Idempotency-Key`.

## 4. Endpunkt-Landkarte

Bereiche in der Reihenfolge, in der sie typischerweise gebraucht
werden. Genaue Bodies, Status-Codes, Beispiele in `v1.md`.

| Bereich | Endpunkte (grob) | v1.md-Sektion | Erforderliche Scope(s) |
|---|---|---|---|
| **Health** | `GET /v1/health`, `GET /v1/internal/health` | 3.1, 3.2 | keine / `admin:*` |
| **Auth** | `POST /v1/auth/token`, `DELETE /v1/auth/token` | 2.1, 2.2 | `admin:*` (für POST) / Self oder `admin:*` (für DELETE) |
| **Instruments** | `GET /v1/instruments/search`, `GET /v1/instruments/{conid}` | 4.1, 4.2 | `instruments:read` |
| **Exchanges** | `GET /v1/exchanges`, `GET /v1/exchanges/{exchange_id}/calendar` | 4.3 (neu, AP-11 K4) | `instruments:read` |
| **Quotes** | `GET /v1/quotes/snapshot`, `GET /v1/quotes/stream` | 5.1, 5.2 | `quotes:read` |
| **Portfolio** | `GET /v1/portfolio/{accountId}`, `.../positions`, `.../ledger` | 6.1, 6.2, 6.3 | `portfolio:read` |
| **Orders** | `POST /v1/orders`, `GET /v1/orders/{id}`, `DELETE /v1/orders/{id}` | 7.1, 7.2, 7.3 | `orders:write` |
| **Trades** | `GET /v1/trades`, `GET /v1/trades/aggregates` | 8.1, 8.2 | `portfolio:read` |
| **Events** | `GET /v1/events/stream` | 9 | `events:read` |
| **Metrics (intern)** | `GET /metrics` | 12 | keine (Compose-internal) |

Die Routen-Wiring liegt in [`src/broker_gateway/main.py`](../src/broker_gateway/main.py)
(`include_router(v1_router)` + `metrics_router`); Modul-Aufteilung in
[`src/broker_gateway/api/v1/`](../src/broker_gateway/api/v1/).

## 5. Stream-Patterns (SSE)

Beide Streams (`/v1/quotes/stream`, `/v1/events/stream`) sind
Server-Sent Events (`Content-Type: text/event-stream`). SSE statt
WebSocket, weil Push-only ohne Bidi-Bedarf reicht und einfaches
HTTP-Tooling reicht. WS-Adapter ist als AP-04 in Discovery (siehe
`02-architecture.md` Sektion 7.2-7.3).

### 5.1 Connect

Standard-SSE-Client genügt. Beispiel-Skizze (nicht normativ — siehe
`v1.md` Section 5.2 / 9 für die Wahrheit):

```python
import httpx

async with httpx.AsyncClient() as client:
    async with client.stream(
        "GET",
        "https://broker-gateway:4000/v1/events/stream",
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        async for line in resp.aiter_lines():
            ...  # SSE-Parsing
```

Real-Code: bibliotheksabhängig (`httpx-sse`, `aiohttp-sse-client`,
JS-EventSource, …). Diese Datei normiert kein Tooling.

### 5.2 Last-Event-ID-Reconnect

Beim Reconnect setzt der Consumer `Last-Event-ID: <id>`. Der Service
liefert ab dem nächsten Event > `id`. Lücken sind möglich, wenn die
Pause länger als der Server-Buffer ist; der Consumer **muss** in dem
Fall idempotent sein. Genauer Mechanismus in `v1.md` Section 5.2.4
und 9.4.

### 5.3 Subscription-Refcount und Fan-Out

Mehrere Consumer für dasselbe Symbol bekommen den **Fan-Out** aus
einer einzigen IBKR-Subscription. Das ist Service-internes Verhalten
und für den Consumer transparent. Konsequenz: keine Sorge wegen
„blockiere ich den anderen Consumer durch mein Subscribe?" — der
Service trennt das.

### 5.4 Backpressure

Service buffert pro Connection. Wenn ein Consumer langsamer liest als
neue Events kommen, droppt der Service die ältesten Events
(`v1.md` Section 9.5). Drop wird als SSE-Comment annotiert und in
`broker_gateway_…` Metriken gezählt. Consumer **muss** bei
Drop-Detection ein Re-Read der unterliegenden Lese-Endpunkte
(`portfolio`, `orders`) triggern, um den State zu rekonstruieren.

### 5.5 Disconnect-Verhalten

| Fall | Service-Verhalten | Consumer-Reaktion |
|---|---|---|
| Netz-Pause | Connection läuft auf TCP-Timeout | Reconnect mit `Last-Event-ID` |
| `auth_lost` | `503` + `Retry-After: 30` | warten + Reconnect |
| Token revoked | `401` mit `error.code: invalid_token` | neuen Token holen, Reconnect |
| Server-Restart | TCP-Reset | Reconnect, ggf. `Last-Event-ID` aus Buffer-Limit überlebt nicht |

## 6. Versionsstrategie aus Consumer-Sicht

### 6.1 Minor-Bumps

`v1.x.y` führt **additive** Felder in Responses ein. Consumer-Code
sollte unbekannte Felder ignorieren (Pydantic-Models mit
`extra="ignore"` oder JSON-Parsing ohne strict-Schema). Kein Update
zwingend.

### 6.2 Patch-Bumps

Reine Bugfixes oder Doku-Änderungen. Kein Vertrag-Effekt.

### 6.3 Major-Bumps

`/v2` nur bei semantischen Änderungen (Default-Werte, Error-Codes,
Retry-Semantik). `/v1` und `/v2` laufen parallel mit
`Deprecation`-Header auf der älteren Version. Consumer planen
Migration im Zeitfenster bis `Sunset`.

### 6.4 Welche Version gilt?

Service-Version aus `GET /v1/health`:

```json
{"status": "ok", "version": "1.11.0"}
```

Consumer **muss** in seiner Spiegel-Doku (Sektion 7) festhalten, gegen
welche Service-Version er gebaut hat — sonst wird Drift unsichtbar.

## 7. Consumer-Gegenpart-Konvention

Jeder Consumer-Repo (PSM, trading-robot, künftige) führt unter

```
docs/integrations/broker-gateway.md
```

eine **Spiegel-Doku** mit folgenden fünf Abschnitten:

### 7.1 Genutzte Scopes mit Begründung

Welche Scope-Namen aus [`docs/04-security.md`](04-security.md) Sektion 3
hat das Token? Pro Scope ein Satz, **warum** er nötig ist (z.B. „PSM
braucht `portfolio:read` für die KESt-FIFO-Berechnung").

### 7.2 Verwendete Endpunkte mit Abruf-Frequenz und Trigger

Tabelle: Endpunkt × Trigger (User-Action / Cron / Stream-Reaktion) ×
typische Frequenz (z.B. „1x pro Minute pro Holding" oder „on-demand").
Ziel: bei Performance-Diskussionen sieht der Service-Owner sofort, wer
welche Last erzeugt.

### 7.3 Annahmen über Response-Felder

Welche Felder liest der Consumer? Welche Annahmen hat er über deren
Bedeutung, Wertebereich, Vorhandensein? Beispiele:

- „PSM nutzt `availability` (`realtime`/`delayed`/`frozen`) und
  fällt bei `frozen` auf yfinance-Snapshot zurück."
- „PSM liest `last`-Preise. Wenn `last` fehlt (Premarket), nutzt er
  `bid`-`ask`-Mitte aus Section 5.1."

Diese Annahmen sind die **Schnittstelle**, gegen die der Consumer
reagiert — ändert das Service-Team additiv ein Feld, sieht der
Consumer-Owner hier sofort, ob es ihn betrifft.

### 7.4 Reaktion auf Fehler und Stream-Disconnect

Welcher Error-Code löst welche Consumer-Aktion aus? Was passiert bei
`auth_lost` / `503` / SSE-Disconnect? Soll der Consumer fail-loud
werden (Alert) oder fallback-still bleiben (Cached-State)?

### 7.5 Verwendete broker-gateway-Version

Genaue Service-Version, gegen die der Consumer aktuell gebaut wurde.
Wird beim Update der Service-Version mit aktualisiert; bei
Bump-Verträglichkeit (Minor / Patch) reicht ein Eintrag im Changelog
des Consumers.

### 7.6 Disziplin

- Spiegel-Doku-Pfad ist **fix**: `docs/integrations/broker-gateway.md`.
- Inhalt ist eine **lokale Wahrheit** des Consumers — nicht zwischen
  Consumer-Repos kopieren.
- Bei Service-API-Änderung (Service-Owner) sendet das Service-Team
  eine Notiz an die Consumer-Owner; jeder Consumer-Owner pflegt sein
  Spiegel-Dok eigenverantwortlich.
- Wenn ein Consumer keine Spiegel-Doku hat, gilt sein Verwendungs-Profil
  als undokumentiert — bei Service-Bumps trifft ihn die Beweislast.

> Die tatsächliche Anlage der Spiegel-Doks in den Consumer-Repos
> (PSM, trading-robot) ist **nicht** Teil dieser Karte. Jeder
> Consumer-Repo führt das per eigener Karte ein.

---

## 8. Verweise und offene Fragen

### 8.1 Verweise

- Formale Spec: [`docs/api/v1.md`](api/v1.md)
- Architektur: [`docs/02-architecture.md`](02-architecture.md)
- Security (Token, Scopes, Vorfall): [`docs/04-security.md`](04-security.md)
- Deployment: [`docs/03-deployment.md`](03-deployment.md)
- Glossar (sobald angelegt): [`docs/06-glossary.md`](06-glossary.md)
- Code: [`src/broker_gateway/api/v1/`](../src/broker_gateway/api/v1/),
  [`src/broker_gateway/main.py`](../src/broker_gateway/main.py)
- Error-Helpers: [`src/broker_gateway/api/v1/errors.py`](../src/broker_gateway/api/v1/errors.py)

### 8.2 Offene Fragen

- **Drift `v1.md` ↔ Implementierung:** Implementation-Status-Tabelle
  in `v1.md` ist auf Service-Version 1.0.0 datiert. Aktuelle Service-
  Version ist 1.11.0; Updates seit dann sind teils nicht in der
  Tabelle reflektiert. Reparatur ist **nicht** Scope dieser Karte (siehe
  Karten-Constraints) — eine eigene Karte „API-Spec-Aktualisierung
  gegen v1.11.0" ist sinnvoll.
- **Eindeutige Section-Nummern:** Verweise „v1.md Section 5.2.4" oben
  funktionieren nur, wenn die Spec eine entsprechende Sub-Sektion
  führt. Bei Drift werden die Verweise stumpf — Pre-Commit-Linter, der
  Anker prüft, ist als offene Frage in [`docs/04-security.md`](04-security.md)
  Sektion 12.2 erwähnt.
- **Spiegel-Dok-Konvention in Consumer-Repos:** Karten in PSM und
  trading-robot anzulegen, sobald die jeweiligen Repos auf v1.11.0
  zugreifen. Heute existieren beide Repos noch nicht in einem
  Stand, der Spiegel-Doks rechtfertigen würde.

---

*Lebt mit dem Service. Die formale Spec ist `docs/api/v1.md` —
Änderungen am Vertrag werden dort gepflegt; dieses Dokument hier
führt nur den Einstieg und die Konsumenten-Konvention.*
