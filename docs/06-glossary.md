# 06 — Glossar

Begriffe aus zwei Domänen, die im Repo, in den Karten und in Logs
nebeneinander auftauchen: **IBKR-Vokabular** (Sektion 1) und
**broker-gateway-Eigenvokabular** (Sektion 2). Jeder Eintrag mit
Kurzdefinition und Verweis auf die Quelle, wo die Tatsache lebt.

> Disziplin: dieses Glossar ist die einzige Stelle, an der Begriffe
> definiert werden. Andere Doks verwenden den Begriff und verweisen
> bei Bedarf hierher — sie wiederholen die Definition nicht.

**Stand:** v1.11.0, 2026-04-30.

---

## 1. IBKR-Vokabular

Alphabetisch.

### `allowedAssetTypes`

Feld im IBKR-Account-Endpoint, das die Asset-Klassen listet, die der
Account-Typ grundsätzlich handeln **könnte** (z.B. `STK,OPT,FUT,...`).
Tatsächliche Trading-Permissions sind eine separate Anfrage pro
Asset-Klasse im IBKR-Portal — der heute aktive Live-Account
(U25235077, Pre-Pro AT) hat Aktien + Cash/FX, alles andere ist
Anfrage-pflichtig. Das geplante dediziertes Service-Konto (siehe
"Service-Konto vs. Privat-Konto" in Sektion 2,
[`docs/02-architecture.md`](02-architecture.md) Sektion 11.2 und
[`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md))
braucht voraussichtlich denselben Permission-Bundle — beim Cutover
zu prüfen, vor dem Recreate.
*Quelle:* [`docs/01-context-from-bootstrap-session.md`](01-context-from-bootstrap-session.md)
Sektion (eingangs zitiert in Memory `project_ibkr_session_owner`).

### Availability-Code (auch: 6509-Code)

String-Code (max. 3 Zeichen) aus IBKR-Quote-Feld 6509 mit der Bedeutung
„Wie aktuell sind diese Marktdaten?". Vollständige Tabelle (inkl.
Sub-Code-Bedeutungen, Empirie, Restunsicherheiten) in
[`docs/research/ibkr-availability-code.md`](research/ibkr-availability-code.md).

Erstes Zeichen kennzeichnet die Datenklasse:

| Praefix | Bedeutung | Adapter-Mapping |
|---------|-----------|-----------------|
| `R` | RealTime (Subscription nötig) | `realtime` |
| `D` | Delayed 15-20 min | `delayed` |
| `Z` | Frozen (Markt geschlossen, Echtzeit-Lieferung des letzten Werts) | `frozen` |
| `Y` | Frozen Delayed | `frozen` |
| `F` | Legacy / Mock (synonym zu Frozen) | `frozen` |
| `N` | Not Subscribed | `None` (Konsument muss defensiv reagieren) |
| `H` | Halted (im WS-Adapter-Tradeability-Layer, nicht im OpenAPI-6509-Schema) | (Adapter `None`; WS-Adapter setzt `current_session=halted`) |

Zweites Zeichen `P` = Snapshot, `p` = Consolidated. Drittes Zeichen
`B` = Book. Empirisch in unseren Recordings (Stand 2026-05-02): nur
`DPB` (62×), `ZB` (3×, ohne mittleres Zeichen), `RPB` (2×).

**Tradeability-Verknuepfung (AP-11 K5):** Der smd-Frame im
WS-Pfad reichert pro Frame zusaetzlich die abgeleiteten Felder
`is_tradeable_now: bool` und `current_session ∈ {rth, pre, post,
closed, halted}` an. Die Wahrheits-Tabelle (siehe
`broker_gateway.cp.tradeability.derive_tradeability`):

- `R*` / `D*` plus aktive Schedule-Session (rth/pre/post)
  ⇒ `is_tradeable_now=true`, `current_session=<session-typ>`.
- `H*` (Halted) ⇒ `is_tradeable_now=false`, `current_session=halted`,
  unabhaengig vom Schedule.
- `Z*` / `Y*` ⇒ Frozen-Familie, ebenfalls `halted`.
- Feiertag oder ausserhalb der Sessions ⇒
  `is_tradeable_now=false`, `current_session=closed`.

`broker-gateway` mappt im Quote-Response auf das Feld `availability`
mit den Werten `realtime` / `delayed` / `frozen`; rohe Code bleibt
zusätzlich für Debug erhalten. *Code-Quelle:*
[`src/broker_gateway/availability.py`](../src/broker_gateway/availability.py),
[`src/broker_gateway/cp/quotes.py`](../src/broker_gateway/cp/quotes.py),
[`src/broker_gateway/cp/tradeability.py`](../src/broker_gateway/cp/tradeability.py).

### Brokerage-Session

IBKR-interner Begriff für die authentifizierte HTTP-Session des
Client-Portal-Gateways. Genau eine pro Konto; jede neue Session kickt
die alte. Service hält sie über Tickle warm.
*Quelle:* `02-architecture.md` Sektion 2.1, Login-Runbook.

### Client-ID

Eindeutige Sub-Identität innerhalb einer IBKR-Session, falls eine
Komponente eine eigene Stream-Holder-Rolle übernimmt. Memory
`IBKR-Streaming Fan-Out` empfiehlt **dedizierte Client-IDs pro
Stream-Holder** mit App-Level-Fan-Out. Heute (`broker-gateway`
Singular-Halter) gibt es genau eine Client-ID pro Service-Instanz.
*Quelle:* Auto-Memory `feedback_ibkr_streaming_fanout` (PSM-Memory,
sinngemäß übertragen).

### `conid` und `conidEx`

`conid` (Contract-ID) ist der numerische IBKR-Identifier eines
Instruments (z.B. AAPL = 265598). `conidEx` ist die erweiterte Form,
die zusätzlich Exchange/Listing-Spezifika enthält und in einigen
Endpoints vorkommt (besonders im First-Call-Snapshot-Stub `[{conidEx,
conid}]`). conids ändern sich praktisch nie — Cache ist langlebig.
*Quelle:* [`src/broker_gateway/cp/instruments.py`](../src/broker_gateway/cp/instruments.py),
`v1.md` Section 4.

### CP-Gateway / Client Portal Gateway

Java-Komponente von IBKR (eclipse-temurin:21-jre + IBKR-Tarball
`clientportal.gw.tar.gz`), die als HTTP-API-Adapter zur IBKR-
Backend-API fungiert. Im `broker-gateway`-Compose-Stack als interner
Service `cpgateway` enthalten, nicht extern publiziert.
*Quelle:* `02-architecture.md` Sektion 4, [`ops/cpgateway/README.md`](../ops/cpgateway/README.md).

### `iserver/*` Endpunkt-Familie

Pfad-Präfix der CP-Gateway-Endpoints, die mit der aktiven Brokerage-
Session arbeiten (im Gegensatz zu `portal/*`-Endpoints, die Konto-
Daten ohne Session liefern). Beispiele: `/iserver/auth/status`,
`/iserver/account/trades`, `/iserver/secdef/search`,
`/iserver/marketdata/snapshot`, `/iserver/reauthenticate`.
*Quelle:* `v1.md` Section 4-9, IBKR-OpenAPI-Spec
[`docs/research/`](research/).

### Ledger

IBKR-Begriff für den Konto-Ledger pro Währung (Cash, Settled-Cash,
Margin-Stand). `broker-gateway` exponiert das als `GET /v1/portfolio/
{accountId}/ledger`.
*Quelle:* `v1.md` Section 6.3.

### MTD-Commission

Sum der Commissions im Month-to-Date-Zeitraum, abgeleitet aus
`/iserver/account/trades?days=30` durch Aufsummieren des
`commission`-Feldes der Trade-Records. Feld hat keine Währungs-
Annotation; bei mehrheitlich US-Aktien plausibel USD.
*Quelle:* `02-architecture.md` Sektion 2.5, `v1.md` Section 8.2.

### Pacing-Violation

IBKR-Fehler, der bei Überschreiten der Rate-Limits (~50 Nachrichten/s
pro Konto, plus pro Endpoint-spezifische Limits) zurückgegeben wird.
Resultiert in HTTP 429 oder Verbindungsabbruch.
*Quelle:* `02-architecture.md` Sektion 2.2, [`src/broker_gateway/throttle/`](../src/broker_gateway/throttle/).

### Reauthenticate

`POST /iserver/reauthenticate` — IBKR-Endpoint, der versucht eine
ausgelaufene Brokerage-Session ohne Browser-2FA zu erneuern. Funktioniert
solange das Backend die Session noch als „suspended" und nicht als
„fully kicked" hält. `broker-gateway` versucht das bis zu 3× im
Auth-Lifecycle, dann kippt der Status auf `auth_lost`.
*Quelle:* [`src/broker_gateway/cp/lifecycle.py`](../src/broker_gateway/cp/lifecycle.py),
Memory `project_ibkr_session_resume`.

### Snapshot vs. Stream

| Modus | Endpoint (CP) | broker-gateway-Endpoint | Frische |
|---|---|---|---|
| **Snapshot** | `GET /iserver/marketdata/snapshot` | `GET /v1/quotes/snapshot` | einmaliger Pull, First-Call leer (siehe First-Call-Prime) |
| **Stream** | WebSocket-Topic `smd` | `GET /v1/quotes/stream` (SSE), Adapter-Stand siehe AP-04 | kontinuierlich, Field-Deltas |

*Quelle:* `v1.md` Section 5, `02-architecture.md` Sektion 7.

### SSO-Validate

`POST /sso/validate` — IBKR-Endpoint, mit dem eine Komponente prüft, ob
ihre Session noch authentifiziert ist (Alternative zu `/iserver/auth/status`).
Wird in Lifecycle-Hooks eingesetzt.
*Quelle:* [`src/broker_gateway/cp/lifecycle.py`](../src/broker_gateway/cp/lifecycle.py).

### Subscription (session-global)

Marktdaten-Subscriptions im CP-Gateway sind **pro Session**, nicht pro
Caller. Wenn Caller A `AAPL` subscribt und Caller B unsubscribt, verliert
A unbemerkt seinen Stream. Limit: ~250 gleichzeitige Streams pro Session,
~5 conids pro Snapshot-Call.
*Quelle:* `02-architecture.md` Sektion 2.3.

### Tickle

`POST /tickle` — Heartbeat-Endpoint. Ohne ~60 s-Tickle läuft die Session
aus. `broker-gateway` betreibt einen `asyncio.Task`-Tickle-Worker.
*Quelle:* `02-architecture.md` Sektion 2.4, [`src/broker_gateway/cp/lifecycle.py`](../src/broker_gateway/cp/lifecycle.py).

### Trades-Endpunkt vs. Orders-Endpunkt

| Begriff | IBKR-Pfad | broker-gateway | Wofür |
|---|---|---|---|
| **Trades** | `/iserver/account/trades?days=30` | `GET /v1/trades` | ausgeführte Executionen (Historie) |
| **Orders** | `/iserver/account/orders` und `/iserver/orders` (place) | `GET /v1/orders/{id}`, `POST /v1/orders` | aktuelle Order-Aufträge (placed/working/filled) |

*Quelle:* `v1.md` Section 7-8.

### `whatif`

IBKR-Pre-Trade-Risk-Check (`POST /iserver/account/{id}/order/whatif`).
Der CP-Gateway antwortet mit Margin-Impact und Warnings 1-99. Speziell
**Warning 1**/`21` („You are trying to submit an order without having
market data") und **Warning 4** („Percentage price check cannot be
performed") tauchen auf, wenn Realtime-Marktdaten für das Instrument
nicht freigeschaltet sind — unabhängig davon, ob der Snapshot-Endpoint
brauchbare Werte liefert. Vollständige Tabelle der bekannten Codes,
Severity-Klassifikation, Empirie und Service-Reaktion in
[`docs/research/ibkr-whatif-warnings.md`](research/ibkr-whatif-warnings.md).
*Quelle:* `02-architecture.md` Sektion 2.5, Research-Doku oben.

### WebSocket-Topics: `smd`, `sor`, `str`

CP-Gateway-WebSocket (`wss://localhost:5000/v1/api/ws`) bietet mehrere
Topic-Kanäle. Die für `broker-gateway` relevanten:

| Topic | Inhalt | Subscribe-Format | Reife (AP-04 K4) |
|---|---|---|---|
| `smd` | Streaming-Marktdaten (Quotes) | `smd+<conid>+{...}` | grün |
| `sor` | Order-Updates (place / fill / cancel) | `sor+{}` | grün |
| `str` | Trades-Stream | `str+{...}` | gelb (Frequenz hoch, Inhalt überprüfungsbedürftig) |
| `spl`, `smh`, `sbd` | Sonstige Topics (price-Live, Symbol-History, Book-Depth) | divers | [OFFENE FRAGE] noch nicht in Discovery |

`broker-gateway` hat den `CPWebSocketClient` als Baustein
(`cp/ws_client.py`); produktiver WS-Adapter ist Sache des Folge-AP-05.
*Quelle:* [`docs/research/ibkr-cpapi-websockets-findings.md`](research/ibkr-cpapi-websockets-findings.md),
`02-architecture.md` Sektion 7.2.

### `6509-Code`

Synonym für **Availability-Code** (siehe oben). Bezieht sich auf das
spezifische IBKR-Quote-Feld mit der Nummer 6509.

---

## 2. broker-gateway-Eigenvokabular

Alphabetisch.

### Cassette

Synonym für eine **Recording**-Datei unter
`tests/fixtures/recorded/`. Eine Cassette ist ein deterministisches
JSON-File pro CP-Gateway-Roundtrip mit Header (gefiltert) und Body
(normalisiert), das beim Mock-Replay als Antwort dient.
*Quelle:* AP-02-Beschreibung („Cassette-Schicht"), [`docs/cp-recordings.md`](cp-recordings.md).

### Service-Konto vs. Privat-Konto

broker-gateway-Begriffspaar für die Konto-Trennung, die mit der
Phase-2-Cutover-Karte aktiv wird. **Privat-Konto** ist das heutige
U25235077 (chmangold), das der Operator parallel im IBKR-Browser und
in der IBKR-App nutzt — jeder Login dort kollidiert mit der broker-
gateway-Live-Session (Single-Session-Constraint
[`02-architecture.md`](02-architecture.md) Sektion 2.1 / 3.1).
**Service-Konto** ist das seit 2026-05-18 beantragte zweite IBKR-Konto,
das ausschließlich broker-gateway-Live hält — gleicher Permission-
Bundle wie heute (Aktien + Cash/FX), keine Operator-Browser-Sessions.
Ziel: Entkopplung der Live-Service-Verfügbarkeit von der Operator-
Aktivität. Der Singular-Halter (3.1) bleibt unverändert; nur die
Identität hinter ihm wechselt.
*Quelle:* [`docs/02-architecture.md`](02-architecture.md) Sektion 3.1 + 11.2,
[`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md),
[`docs/04-security.md`](04-security.md) Sektion 12.2.

### Compose-Project-Name

Docker-Compose-Stack-Identifier (Default: Verzeichnisname). Live-Stack
nutzt `broker-gateway`, Paper-Stack ist als `broker-gateway-paper`
geplant. Über `COMPOSE_PROJECT_NAME` per ENV setzbar — erlaubt
parallele Stacks auf demselben Host.
*Quelle:* `03-deployment.md` Sektion 9, AP-06-Beschreibung.

### Drift-Detection

Zwei Mechanismen, die Schema-Stabilität gegen IBKR sicherstellen:
- **Doku-Drift:** systemd-Timer auf cma-pi-1 zieht täglich die
  IBKR-OpenAPI-Spec und vergleicht gegen
  `docs/research/ibkr-cpapi-doc.json`.
- **Live-Drift (Build-Acceptance):** `scripts/check_mock_drift.py
  --build-acceptance` läuft beim Container-Rebuild als Acceptance-Gate;
  Build schlägt fehl, wenn Mock-Fixture ≠ Live-Antwort.
*Quelle:* AP-03-Beschreibung, `02-architecture.md` Sektion 9.3,
[`ops/build-gateway.sh`](../ops/build-gateway.sh).

### Error-Envelope

Schema-Konvention ab v1.5.0: jede Fehlerantwort hat den Body
`{"error": {"code", "message", "request_id", "retry_after_s",
"extra"}}`. Single Source of Truth: `v1.md` Section 1.6,
Implementierung in [`src/broker_gateway/api/v1/errors.py`](../src/broker_gateway/api/v1/errors.py).

### First-Call-Prime

Workaround für IBKR-Eigenheit: erster Snapshot-Call liefert
`[{conidEx, conid}]` ohne Werte; erst der zweite Call (~3 s später)
liefert reale Daten. `broker-gateway` führt deshalb intern **zwei**
Calls aus und gibt nur den zweiten an den Consumer weiter.
*Quelle:* `02-architecture.md` Sektion 2.5 + 5,
[`src/broker_gateway/cp/quotes.py`](../src/broker_gateway/cp/quotes.py).

### Idempotency-Key / Idempotency-Map

Header `Idempotency-Key: <UUID>` ist Pflicht für `POST /v1/orders` und
`DELETE /v1/orders/{id}`. Der Service hält eine **In-Memory-Map**
`key -> response` mit Default-TTL 24 h
(`BG_IDEMPOTENCY_TTL_S`-konfigurierbar). Wiederholungen liefern die
gespeicherte Response.
*Quelle:* `02-architecture.md` Sektion 3.4, `04-security.md` Sektion 9,
[`src/broker_gateway/idempotency.py`](../src/broker_gateway/idempotency.py).

### Money-Adapter / Money-Normalisierung

Konvention seit v0.8.0: jedes Geldfeld in Portfolio-, Order- und
Trades-Bodies hat die Form `{"value": <decimal>, "currency": <ISO-4217>}`.
Wandlung zentral in [`src/broker_gateway/cp/normalize.py`](../src/broker_gateway/cp/normalize.py)
und [`src/broker_gateway/money.py`](../src/broker_gateway/money.py).
*Quelle:* `v1.md` Section 6 + 7, CHANGELOG v0.8.0.

### Pacing-Header

**Stand-Audit AP-09 (Mai 2026):** teilweise implementiert.

| Header | Implementiert? | Wo |
|--------|----------------|----|
| `Retry-After` (RFC 7231) | **ja** — bei `429 Too Many Requests` und bei `503` mit `auth_lost`/`cp_down`. | [`src/broker_gateway/cp/lifecycle.py`](../src/broker_gateway/cp/lifecycle.py) (503), [`src/broker_gateway/api/v1/quotes_stream.py`](../src/broker_gateway/api/v1/quotes_stream.py) (429), [`src/broker_gateway/api/v1/errors.py`](../src/broker_gateway/api/v1/errors.py) (Error-Envelope). |
| `X-RateLimit-Remaining`, `X-RateLimit-Reset` | **nein** — nicht im Code-Pfad. | n/a |
| `X-Pacing-Wait` | **nein** — nicht im Code-Pfad. | n/a |

Konsumenten lesen heute ausschliesslich `Retry-After` (das ist nach
RFC 7231 die normative Pacing-Information). Detaillierte Token-Bucket-
Metriken sind nicht im Response-Header, sondern als Prometheus-
Metriken sichtbar:
`broker_gateway_pacing_violations_total`,
`broker_gateway_throttle_extra_wait_seconds`. *Quelle:* `v1.md`
Section 11, [`src/broker_gateway/throttle/`](../src/broker_gateway/throttle/).

### Recording / Replay

**Recording**: live-Aufzeichnung von CP-Gateway-Roundtrips über einen
httpx-Event-Hook in [`src/broker_gateway/cp/recorder.py`](../src/broker_gateway/cp/recorder.py),
aktiviert durch `BG_CP_RECORD_DIR`. Header werden gefiltert
(Redaction), Bodies normalisiert (Timestamps/IDs zu Platzhaltern).

**Replay**: Mock-Fixture in [`tests/conftest.py`](../tests/conftest.py)
+ `tests/cp_mock/replay.py` lädt die Recordings (`live/` > `seed/`
Vorrang) und gibt sie als Mock-Antworten zurück.
*Quelle:* AP-02-Beschreibung, [`docs/cp-recordings.md`](cp-recordings.md).

### Redaction (Header-Redaktion)

Single-Source-of-Truth-Liste der Header-Namen, die vor jedem
Logging/Recording-Sink gefiltert werden:
`Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, `X-Auth-Token`,
`Proxy-Authorization`. Lebt in [`src/broker_gateway/cp/redaction.py`](../src/broker_gateway/cp/redaction.py)
mit Tests in [`tests/test_cp_redaction.py`](../tests/test_cp_redaction.py).
*Quelle:* `04-security.md` Sektion 4, AP-05-Beschreibung.

### Refcount (Subscription-Refcount)

Service-internes Zählen, wie viele Consumer pro `conid` aktuell ein
Quote-Stream-Abo haben. Solange der Refcount ≥ 1 ist, bleibt die
**eine** IBKR-Subscription aktiv und der Service-interne Fan-Out
bedient alle Consumer aus dieser einen Quelle.
*Quelle:* `02-architecture.md` Sektion 7.1,
[`src/broker_gateway/streams/manager.py`](../src/broker_gateway/streams/manager.py).

### `request_id` (Korrelations-ID)

Pro Inbound-Request gesetzte UUID, gebunden an den structlog-Context
(`structlog.contextvars.bind_contextvars`). Erscheint automatisch in
jedem nachgelagerten Log-Event derselben Verarbeitung — `inbound.log`,
`cp_wire.log` (kommend AP-05 K3), `app.log`. Erlaubt forensische
Korrelation von Consumer-Request bis IBKR-Roundtrip.
*Quelle:* `02-architecture.md` Sektion 8.2, AP-05-Beschreibung,
[`src/broker_gateway/middleware/observability.py`](../src/broker_gateway/middleware/observability.py).

### Singular-Halter

Architektur-Prinzip 3.1 (Architektur-Doku): es gibt genau **eine**
Instanz von `broker-gateway` pro IBKR-Konto. Skalierung passiert auf
Consumer-Seite, nicht hier — folgt aus dem IBKR Single-Session-Constraint.
*Quelle:* `02-architecture.md` Sektion 3.1.

### Stateless-aussen / Stateful-innen

Architektur-Prinzip 3.2: nach außen verhält sich `/v1` wie ein
klassischer REST-Service (jeder Request self-contained); innen hält
der Service Auth-Session, Subscription-State, Idempotency-Map,
Order-Cache, Throttle-Buckets.
*Quelle:* `02-architecture.md` Sektion 3.2.

### Stream-Fan-Out

Service-internes Verteilen von Quote-/Event-Frames aus **einer**
IBKR-Subscription auf mehrere Consumer-SSE-Connections. Konsequenz aus
Singular-Halter + Subscription-Session-global.
*Quelle:* `02-architecture.md` Sektion 7.1, AP-04-Beschreibung.

### Throttle / Token-Bucket

Pacing-Mechanik pro Endpoint-Klasse (Snapshot anders als Orders anders
als Subscriptions). Implementiert als Token-Bucket je Klasse mit
Burst-Limit und Refill-Rate; Über-Limits führen zu internem Warten,
nicht zu HTTP 429 nach außen.
*Quelle:* AP-01 K11 (Throttle), [`src/broker_gateway/throttle/`](../src/broker_gateway/throttle/),
`v1.md` Section 11.

### Tickle-Worker

`asyncio.Task` in [`src/broker_gateway/cp/lifecycle.py`](../src/broker_gateway/cp/lifecycle.py),
der alle 60 s (`BG_CP_TICKLE_INTERVAL_S`) ein `POST /tickle` an den
internen CP-Gateway sendet. Bei Fehlschlag versucht er bis zu 3×
`reauthenticate`, danach kippt der Service-Status auf `auth_lost`.
*Quelle:* `02-architecture.md` Sektion 5, README-Sektion „CP-Gateway-
Auth-Lifecycle".

### Versionierung-am-Contract

Architektur-Prinzip 3.3: `/v1` bleibt rückwärtskompatibel; additive
Felder in Responses sind erlaubt; Breaking Changes ausschließlich in
`/v2`. Service-Code-Versionen (`pyproject.toml`) bumpen unabhängig
davon — der Vertrag der API ist stabil, der Code hinter ihm darf sich
ändern.
*Quelle:* `02-architecture.md` Sektion 3.3, `05-api.md` Sektion 6.

---

## 3. Verweise und offene Fragen

### 3.1 Verweise

Begriffe, die in den anderen Doks auftauchen, aber nur hier definiert
werden. Ein Hinweis am Anfang von `02-architecture.md` /
`04-security.md` / `05-api.md` macht das transparent — Glossar-
Einträge werden aber nicht bei jedem Auftreten verlinkt.

### 3.2 Offene Fragen

Begriffe, deren Bedeutung beim Schreiben nicht eindeutig festgestellt
werden konnte und in der Tabelle oben mit `[OFFENE FRAGE]` markiert
sind:

- ~~Vollständige Tabelle der Availability-Sub-Codes (zweites/drittes
  Zeichen jenseits `P`/`B`).~~ **geklärt** in
  [`docs/research/ibkr-availability-code.md`](research/ibkr-availability-code.md)
  (AP-09, Mai 2026).
- ~~Genauer Semantik-Block des `F` (Frozen) im Availability-Code.~~
  **geklärt** im selben Research-Doku.
- ~~whatif-Warning-Code-Tabelle 1-99 (heute nur Warnings 4 + 21
  empirisch bekannt).~~ **kuratiert konsolidiert** in
  [`docs/research/ibkr-whatif-warnings.md`](research/ibkr-whatif-warnings.md)
  (AP-09, Mai 2026). Vollständige 1-99-Tabelle bleibt offen, da IBKR
  sie nicht zentral dokumentiert; neue Codes werden bei Auftritt in
  diesem Research-Doku ergänzt.
- WebSocket-Topics jenseits von `smd`/`sor`/`str` (`spl`, `smh`,
  `sbd`) — Inhalt und Subscribe-Format noch nicht in AP-04-Discovery.
- ~~Pacing-Header-Implementierungs-Stand (spezifiziert in v1.md Section
  11; Live-Stand unklar).~~ **geklärt** (AP-09, Mai 2026): nur
  `Retry-After` ist live; `X-RateLimit-*` und `X-Pacing-Wait` sind
  spezifiziert, aber nicht implementiert. Details im
  Glossar-Eintrag oben. Folge-Hardening (RateLimit-Header live
  schalten) bleibt als optionale Karte offen — kein Blocker fuer
  Consumer, die `Retry-After` lesen.

Diese Fragen werden über separate Karten geklärt — die Antworten
fließen dann hier ein, nicht woanders.

---

*Lebt mit dem Service. Karten, die neue Begriffe einführen, ergänzen
oder schärfen, aktualisieren dieses Glossar entsprechend.*
