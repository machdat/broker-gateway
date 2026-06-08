# 04 — Security

Konsolidierte Sicherheits-Konventionen für `broker-gateway`. Single Source
of Truth für Token-Modell, Scope-Matrix, Header-Redaktion, Body-Logging-
Regeln, Recording-Disziplin, 2FA-Lifecycle gegenüber IBKR, Netzwerk-Setup
und Vorfall-Reaktion.

> Architektur-Sicht (Was) liegt in [`docs/02-architecture.md`](02-architecture.md)
> Sektion 6 (Auth) und 8 (Logging). Hier dokumentieren wir das Wie und die
> Threat-Sicht. Bei Überschneidung verweist eine Datei auf die andere.
> **Begriffsklärungen:** [`docs/06-glossary.md`](06-glossary.md).

**Stand:** v1.11.0, 2026-04-30.

## Inhalt

1. [Threat-Modell](#1-threat-modell)
2. [Token-Modell](#2-token-modell)
3. [Scope-Matrix](#3-scope-matrix)
4. [Header-Redaktion](#4-header-redaktion)
5. [Body-Logging-Regeln](#5-body-logging-regeln)
6. [Recording-Disziplin](#6-recording-disziplin)
7. [2FA-Lifecycle gegenüber IBKR](#7-2fa-lifecycle-gegenueber-ibkr)
8. [Netzwerk-Sicherheit](#8-netzwerk-sicherheit)
9. [Idempotency-Sicherheit](#9-idempotency-sicherheit)
10. [Reaktion auf Vorfall](#10-reaktion-auf-vorfall)
11. [Was bewusst NICHT in v1 abgedeckt](#11-was-bewusst-nicht-in-v1-abgedeckt)
12. [Verweise und offene Fragen](#12-verweise-und-offene-fragen)

---

## 1. Threat-Modell

Skizze, gegen welche Klassen von Angriffen oder Pannen `broker-gateway`
heute schützt — und welche bewusst aus dem Scope sind.

| Bedrohung | Schutz im Service | Restrisiko |
|---|---|---|
| **Single-Session-Hijack** (zwei Caller killen sich gegenseitig die IBKR-Session) | Singular-Halter (Architektur-Sektion 3.1): genau eine Instanz pro Konto, Consumer sehen IBKR nicht. | Operator startet versehentlich eine zweite Instanz mit demselben Konto. |
| **Token-Diebstahl im Transit** | Bearer-Token nur über interne Tailscale-Verbindung (Sektion 8). Header-Redaktion verhindert Loggen / Recorden. | Compromise des Hosts, des Operator-Laptops oder der Tailscale-Identität. |
| **Token-Diebstahl in Logs / Recordings** | `Authorization`/`Cookie`/`Set-Cookie`/`X-API-Key`/`X-Auth-Token`/`Proxy-Authorization` werden vor jedem Sink (Recorder, Inbound-Log, kommender CP-Wire-Hook) gefiltert. Tokens stehen niemals in Bodies. | Eintrag eines Tokens als Klartext in einen Body durch Programmierfehler — wird durch SSOT [`src/broker_gateway/cp/redaction.py`](../src/broker_gateway/cp/redaction.py) und Tests in [`tests/test_cp_redaction.py`](../tests/test_cp_redaction.py) abgesichert, aber nicht durch einen automatischen Body-Scan. |
| **Replay von Schreiboperationen** (Order-Resubmit nach Netz-Retry) | Idempotency-Key-Pflicht für Schreiboperationen, In-Memory-Map mit konfigurierbarer TTL (Default 24 h). | Replay nach Service-Restart (Memory-Map ist transient, siehe Sektion 9). |
| **Compromise des CP-Gateway-Containers** (Eskalation aus Java-Prozess) | `cpgateway`-Container läuft als non-root-User `cpgw`, kein extern publizierter Port, eingegangener Traffic ausschließlich über internes Compose-Netzwerk. | Schwachstelle in der IBKR-Java-Komponente selbst — keine Mitigation außer Tarball-SHA256-Pinning. |
| **Recording-Leak** (Tarballs/Fixtures wandern in Issues, PRs, Sharing) | Recorder filtert Header und normalisiert nicht-deterministische Felder (Timestamps, Order-IDs, Session-IDs) vor dem Schreiben. Pre-Commit-Hook `scripts/pre_commit_recording_scan.py` (AP-05 K4) scannt staged Recording-Dateien zusätzlich auf Token-Heuristiken. | Hook ist erst aktiv nach `pre-commit install` pro Clone — Operator-Disziplin bleibt nötig. |
| **Tarball-Tampering** (CP-Gateway-Tarball aus IBKR ausgetauscht) | SHA256-Verifikation des Tarballs im Image-Build, hard fail. | Wenn der ursprünglich vertraute SHA selbst kompromittiert ist (Supply-Chain). |

Aus dem Scope: DDoS-Schutz auf Transport-Ebene (Tailscale-Reach genügt),
RBAC jenseits der einfachen Scope-Matrix, Audit-Trail mit revisions-
sicherer Speicherung, Verschlüsselung-at-rest für die Token-Datei.

---

## 2. Token-Modell

### 2.1 Format

- **Opaque** Bearer-Tokens, kein JWT. Kein parsbarer Inhalt — nur
  Lookup im Store.
- Generiert serverseitig per `secrets.token_urlsafe(32)`
  (~43 URL-safe Zeichen). Quelle: [`src/broker_gateway/auth/store.py`](../src/broker_gateway/auth/store.py)
  (`generate_token_value`).
- Felder pro Token (siehe [`src/broker_gateway/auth/models.py`](../src/broker_gateway/auth/models.py)):
  `value`, `caller_id`, `scopes`, `created_at`, optional `expires_at`.
- Pydantic-Validator stellt sicher, dass nur **bekannte** Scope-Strings
  akzeptiert werden — unbekannte Scopes werden bei `POST /v1/auth/token`
  abgewiesen. Scope-Konstanten leben ausschließlich in
  [`src/broker_gateway/auth/models.py`](../src/broker_gateway/auth/models.py).

### 2.2 Storage

| Backend | Aktivierung | Eigenschaften |
|---|---|---|
| `InMemoryTokenStore` | Kein `BG_TOKEN_FILE`-ENV — kein Compose-Default, nur Fallback wenn jemand das ENV explizit auf leer setzt. | Thread-sicher, verliert State beim Neustart. Konsistent mit dem transienten Service-Charakter. |
| `FileTokenStore` | **Compose-Default ab v2.1.4** via `BG_TOKEN_FILE=/var/lib/broker-gateway/tokens.json` und Volume-Mount `${BG_TOKEN_DIR_HOST:-./var/broker-gateway}:/var/lib/broker-gateway`. | JSON, atomare Writes (temp-file + `os.replace`). Nur lesen/schreiben durch den Service-User. |

Bis v2.1.3 mussten Operatoren `BG_TOKEN_FILE` und einen Volume-Mount
manuell ergänzen. Ab v2.1.4 (Karte `b05206c7`) ist der `FileTokenStore`
Compose-Default, weil die kombinierte Klasse aus „InMemoryStore + lang
laufender Service" Operatoren in die Falle gelaufen ist: Token wird
erzeugt, Service wird Wochen später für ein Image-Update neugestartet,
und alle Konsumenten-Tokens sind weg — der bootstrap-`admin:*`-Token
kommt aus dem ENV zurück, der Rest nicht. Auf cma-pi-1 zeigt
`BG_TOKEN_DIR_HOST` standardmäßig auf `/var/lib/broker-gateway` (Mode
0700 root:root), das Container-File auf 0600 nach dem ersten `put()`.

Persistenz im File-Backend ist ausschließlich `tokens.json`. Der
`FileTokenStore` (AP-10 K1, ab v1.15.0) prüft beim Init die
Datei-Permissions: jeder lesbare/schreibbare/ausführbare Zugriff für
`group` oder `other` (`S_IRGRP|S_IWGRP|S_IXGRP|S_IROTH|S_IWOTH|S_IXOTH`)
löst eine `WARNING` im `app.log`-Strang mit konkretem `chmod 0600`-Hinweis
aus. Neue/überschriebene Token-Dateien werden vor dem atomaren Rename
explizit per `os.chmod(..., 0o600)` abgesichert — nach dem ersten
`put()` ist das File auf POSIX dauerhaft auf 0600. Auf Windows ist die
Pruefung übersprungen (NTFS-Inheritance, kein verlässlicher
POSIX-mode). Crash-frei: zu offene Permissions sind eine Warnung, kein
Service-Stop, weil legitime Setups (z.B. Backup-Tool mit Group-Read)
existieren können.

Andere Backends (Redis, SQL) sind über das `TokenStore`-Protocol
denkbar, aber nicht implementiert.

### 2.3 Bootstrap-Token

`BG_BOOTSTRAP_ADMIN_TOKEN` (ENV) wird beim App-Start in den Store
geschrieben mit Scope `admin:*`. Der Wert kommt nur aus dem Environment,
**nie** aus dem Repo oder einem eingecheckten Compose-File. `.env`
liegt lokal und ist gitignored. Ohne diesen Token kann der Service zwar
starten und `/v1/health` beantworten, aber keine geschützten
Endpunkte (inkl. `/v1/internal/health`) bedienen.

Empfohlene Erzeugung:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

### 2.4 Rotation

- Neuen Token erzeugen: `POST /v1/auth/token` mit Admin-Token-Header.
- Alten Token revoken: `DELETE /v1/auth/token` (Self oder Admin).
- Konsumenten lesen ihren Token ausschließlich aus ihrer eigenen
  Konfiguration (PSM, trading-robot — eigenes Konfig-Repo, ggf. Secrets-
  Manager). Es gibt keine zentrale Rotation; Operator triggert sie
  bewusst.

#### 2.4.1 Konkretes Rezept: Scope-Vergabe / -Erweiterung

Tokens sind immutable — eine Scope-Erweiterung ist immer ein neuer
Token + Revoke des alten. Das Token-Modell kennt absichtlich kein
PATCH-Endpoint, weil das die Threat-Sicht („was darf dieser Wert?")
auf einen einzigen Lifetime-Lookup reduziert.

Für eine Vergabe oder Scope-Erweiterung an einen Konsumenten auf
cma-pi-1:

```bash
# 1. Admin-Token aus dem laufenden Container-ENV holen
TOKEN_ADMIN=$(docker inspect broker-gateway \
    --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | awk -F= '/^BG_BOOTSTRAP_ADMIN_TOKEN=/{print $2}')

# 2. Neuen Token mit Ziel-Scopes erzeugen
curl -sS -X POST http://localhost:4000/v1/auth/token \
    -H "Authorization: Bearer $TOKEN_ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"caller_id":"psm","scopes":["instruments:read","quotes:read"]}'
# Antwort enthaelt das Feld "value" - das ist der Bearer-Token-Wert.

# 3. Token-Wert in eine consumer-eigene Default-Datei auf dem Pi schreiben
#    (Mode 0640 root:cma, damit der Operator per scp/SSH lesen kann).
sudo install -m 0640 -o root -g cma /dev/null /etc/default/broker-gateway-psm.env
sudo tee /etc/default/broker-gateway-psm.env <<'EOF'
# Wert des PSM-Tokens (BROKER_GATEWAY_TOKEN im PSM-.env).
# Erzeugt: <ISO-Datum>; Scopes: instruments:read, quotes:read.
BROKER_GATEWAY_TOKEN=<VALUE_AUS_POST_/v1/auth/token>
EOF

# 4. Konsument (PSM/.env) auf den neuen Wert umstellen, dann testen.
curl -sS -H "Authorization: Bearer <neuer-token>" \
    http://cma-pi-1:4000/v1/quotes/snapshot?conid=265598
# 200 -> Scopes greifen. 403 missing_scope -> Vergabe schief.

# 5. Alten Token revoken (per Wert, mit Admin-Token):
curl -sS -X DELETE \
    "http://localhost:4000/v1/auth/token?value=<ALTER_TOKEN_WERT>" \
    -H "Authorization: Bearer $TOKEN_ADMIN"
# 204 -> revoked. 404 -> Wert nicht im Store (z.B. nach Service-Restart
# unter v2.1.3 InMemoryStore - dann ist er sowieso schon weg).
```

Tokens-Werte landen niemals in einem Karten-Log, einem Commit oder
einem PR-Body — die Default-Datei auf dem Pi (`/etc/default/broker-
gateway-{consumer}.env`, 0640 root:cma) ist die einzige
nicht-ephemere Spur außerhalb des `FileTokenStore`.

Folge-Scopes (`events:read`, `portfolio:read`, `orders:write`) folgen
demselben Rezept — `scopes`-Liste im POST anpassen, Konsumenten-
Default-Datei wegen Audit aktualisieren.

### 2.5 Revocation

`DELETE /v1/auth/token` entfernt den Token aus dem Store. Lookups danach
liefern HTTP 401 mit `error.code: invalid_token`. Im File-Backend wird
der Eintrag atomar aus `tokens.json` entfernt.

Bei Verdacht auf Compromise: sofort revoken; siehe Sektion 10 für die
volle Reaktionskette.

---

## 3. Scope-Matrix

Single Source of Truth für Scope-Namen: [`src/broker_gateway/auth/models.py`](../src/broker_gateway/auth/models.py)
(`SCOPE_*`-Konstanten und `ALL_SCOPES`).

### 3.1 Scopes

| Scope | Was darf der Token-Halter? | Wo geprüft? |
|---|---|---|
| `instruments:read` | `/v1/instruments/search`, `/v1/instruments/{conid}` | `Depends(require_scope(SCOPE_INSTRUMENTS_READ))` |
| `quotes:read` | `/v1/quotes/snapshot`, `/v1/quotes/stream` | analog |
| `portfolio:read` | `/v1/portfolio/{accountId}/*`, `/v1/trades` | analog |
| `events:read` | `/v1/events/stream` | analog |
| `orders:read` | `GET /v1/orders/{id}` (Order-Status; auch mit `orders:write` erreichbar) | `Depends(require_any_scope(SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE))` |
| `orders:write` | `POST /v1/orders`, `DELETE /v1/orders/{id}` | analog |
| `admin:*` | Token-Verwaltung; matcht **alle** Scope-Checks | analog (Wildcard in `Token.has_scope`) |

`admin:*` umgeht jede Scope-Prüfung — wer einen Admin-Token bekommt,
kann alles. Token mit `admin:*` werden ausschließlich für Operator-
Aufgaben (Token-Erzeugen, Internal-Health, Notfall-Reauth) angelegt
und sollten kurzlebig sein.

### 3.2 Konsumenten-Mapping

| Consumer | Erwartete Scopes | Begründung |
|---|---|---|
| **personal_stock_manager (PSM)** | `instruments:read`, `quotes:read`, `portfolio:read` | Lese-Konsument; kein Trade-Auftrag, kein Event-Stream nötig (heute) |
| **trading-robot** | `instruments:read`, `quotes:read`, `portfolio:read`, `orders:write`, `events:read` | Auto-Trader, braucht alle Lesepfade plus Schreib-Rechte und Push-Events |
| **Admin / CLI / Notebooks** | `admin:*` (kurzlebig) oder gezielt einzelne Scopes | Diagnose, Token-Rotation, Service-Health-Checks |

Die Matrix ist nicht im Code zementiert — Operator wählt Scopes beim
`POST /v1/auth/token`-Call. Pydantic erzwingt nur die Mengen-Constraint.

---

## 4. Header-Redaktion

### 4.1 SSOT

[`src/broker_gateway/cp/redaction.py`](../src/broker_gateway/cp/redaction.py)
führt die `frozenset` `REDACTED_HEADERS`. **Keine Datei darf eine eigene
Liste pflegen.** Module-Doc-String macht das explizit.

Aktuell redacted (case-insensitive):

| Header | Warum |
|---|---|
| `authorization` | Bearer-Token des Consumers |
| `cookie` | CP-Gateway-Session-Cookie |
| `set-cookie` | dito (Response-Richtung) |
| `x-api-key` | optionale API-Keys (Defense-in-Depth) |
| `x-auth-token` | Custom-Auth-Header, falls eingeführt |
| `proxy-authorization` | falls je ein Proxy zwischenfunkt |

`filter_headers()` arbeitet auf `httpx.Headers`, `dict[str, str]` oder
einer beliebigen Mapping-ähnlichen Sequenz. Der Aufrufer entscheidet,
wo er filtert — typisch unmittelbar vor `JsonRenderer`/`json.dump`.

### 4.2 Verwender (heute)

| Modul | Wozu |
|---|---|
| [`cp/recorder.py`](../src/broker_gateway/cp/recorder.py) | filtert Request- und Response-Header vor dem Schreiben einer Recording-Fixture |
| `middleware/observability.py` (Inbound-Body-Logging, AP-05 K2) | filtert Request-/Response-Header vor dem `inbound.log`-Event |
| `broker_gateway.cp.wire_log.CPWireLogger` (AP-05 K3) | filtert vor `cp_wire.log` |

### 4.3 Test-Disziplin

[`tests/test_cp_redaction.py`](../tests/test_cp_redaction.py) prüft
`filter_headers()` direkt. [`tests/test_cp_recorder.py`](../tests/test_cp_recorder.py)
und [`tests/test_observability.py`](../tests/test_observability.py)
prüfen, dass die jeweiligen Sinks die SSOT verwenden — d.h. `Authorization`
& Co. tatsächlich nicht in geschriebenen Bodies/Events landen.

[`tests/test_no_token_leak_in_bodies.py`](../tests/test_no_token_leak_in_bodies.py)
(AP-05 K5) erzeugt einen frischen Bearer-Token und prüft, dass dessen
value in keinem Body und keinem Response-Header der Read-Endpunkte
auftaucht. Abgedeckt sind `/v1/health`, `/v1/internal/health`,
`/v1/instruments/search`, `/v1/instruments/{conid}`,
`/v1/quotes/snapshot`, `/v1/portfolio/{accountId}` (Summary,
Positions, Ledger), `/v1/orders/{order_id}` und `/v1/trades`.
`POST /v1/auth/token` ist explizit ausgenommen — der Token-Echo ist
dort designiertes Verhalten, ein zweiter Test schützt diese
Auslassung gegen Drift. Stream-Endpunkte (SSE) sind out-of-scope
dieser Karte und brauchen eine separate Mechanik (siehe AP-05-Notiz).

---

## 5. Body-Logging-Regeln

### 5.1 Was wird geloggt

| Strang | Bodies geloggt? | Notation |
|---|---|---|
| `inbound.log` (Consumer → Gateway) | **Ja**, 1:1 (keine Redaction, keine Truncation), ENV-Schalter `BG_LOG_INBOUND_BODIES` | JSON-Struktur als `request_body`/`response_body` Dict (bei `application/json`), sonst UTF-8-String, sonst `null` mit `_b64`-Fallback |
| `cp_wire.log` (Gateway → IBKR) | **Ja**, 1:1 (keine Normalisierung), ENV-Schalter `BG_CP_WIRE_LOG` (Default `on`) | analog inbound |
| `app.log` (Lifecycle, Throttle, Streams) | nur Metadaten | strukturierte Felder pro Logger |
| Recordings (`tests/fixtures/recorded/`) | **Ja**, 1:1, durchläuft `cp.normalize.normalize_response` (Timestamps/IDs werden zu Platzhaltern) | JSON pro Roundtrip |

SSE-Antworten (`text/event-stream`) werden **nicht** materialisiert:
`response_streaming: true`, `response_body: null`. Sonst würde der
Stream durch das Logging blockiert.

### 5.2 Regeln

- **Tokens stehen niemals in Bodies, niemals in Logs, niemals in
  Recordings.** Token-Werte werden ausschließlich im Authorization-
  Header übertragen, der ist redacted. Tokens in JSON-Bodies sind ein
  Programmierfehler — wird durch Code-Review verhindert.
- Header laufen durch `filter_headers()` (Sektion 4) bevor sie den Sink
  erreichen.
- `caller_id` und `scopes` werden geloggt (das ist der Zweck des
  Trails) — nie der Token-Wert selbst.
- Notfall-Schalter `BG_LOG_INBOUND_BODIES=off` deaktiviert Body- und
  Header-Felder im `http_request`-Event; Metadaten bleiben unverändert.
  Trigger: Bodies werden zu groß oder enthalten unerwartete sensitive
  Daten.

### 5.3 Korrelation

`request_id` wird per `structlog.contextvars.bind_contextvars` gesetzt
und erscheint automatisch in jedem Event derselben Verarbeitung. Damit
korrelieren Inbound- und CP-Wire-Roundtrips über die request_id —
ohne dass ein Token oder anderer Sensitive-Wert im Korrelations-Pfad
landet.

---

## 6. Recording-Disziplin

### 6.1 Aktivierung

Recorder schreibt nur bei gesetztem `BG_CP_RECORD_DIR`. Im Default
(Live-Stack ohne diese ENV) entsteht keinerlei Disk-IO. Aktivierung
also bewusst pro Recording-Session, nicht im Default-Service-Lauf.

Konzept und Diff-Bewertung: [`docs/cp-recordings.md`](cp-recordings.md).
Implementierung: [`src/broker_gateway/cp/recorder.py`](../src/broker_gateway/cp/recorder.py).

### 6.2 Was wird vor dem Schreiben gemacht

1. **Header-Filter** durch `filter_headers()` (Sektion 4) — `Authorization`,
   `Cookie`, `Set-Cookie`, `X-API-Key`, `X-Auth-Token`, `Proxy-Authorization`
   verschwinden.
2. **Body-Normalisierung** durch [`cp/normalize.py`](../src/broker_gateway/cp/normalize.py)
   `normalize_response`: Timestamps und Order-/Execution-/Session-IDs
   werden durch Platzhalter ersetzt. Damit bleiben Fixtures byte-identisch
   über mehrere Live-Sessions hinweg — nötig für die Drift-Detection.

### 6.3 Live-Recordings sind Repo-Inhalt

`tests/fixtures/recorded/live/` ist eingecheckt — die Dateien sind die
kanonische Mock-Quelle (Architektur-Sektion 9.1, 9.2). Damit sie das
**bleiben**, scannt der Pre-Commit-Hook
[`scripts/pre_commit_recording_scan.py`](../scripts/pre_commit_recording_scan.py)
jede staged JSON/JSONL unter `tests/fixtures/recorded/` auf:

- Header-Namen aus `REDACTED_HEADERS` (Single Source of Truth in
  [`src/broker_gateway/cp/redaction.py`](../src/broker_gateway/cp/redaction.py)) —
  `Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, `X-Auth-Token`,
  `Proxy-Authorization`, case-insensitive.
- URL-safe-Strings ≥ 32 Zeichen aus `[A-Za-z0-9_-]` (Token-Heuristik).
  Allowlist für bekannte Hash-/Identifier-Felder (`MAC`,
  `hardware_info`, `etag`, `server-timing`, `x-request-id`,
  `request_id`, `user-agent`, Manifest-`files`-Listen,
  whatif-`warns`/`warning_code`/`warning_message`).
- Cookie-Patterns als Substring (`sess=`, `X-XSRF-TOKEN=`, `_csrf=`,
  `JSESSIONID=`).

Aktivierung pro Clone einmalig: `pip install -e .[dev] && pre-commit install`.
Manueller Voll-Lauf: `pre-commit run --all-files`. WS-Recordings unter
`tests/fixtures/recorded/ws/` skippen die URL-safe-Heuristik (Frame-
und Session-IDs sind dort strukturell URL-safe-32-stellig); Header-
und Cookie-Patterns bleiben aktiv. Bei Recording-Sessions ist die
Sichtprüfung dadurch automatisiert; manuelle Verifikation bleibt
empfehlenswert, ersetzt den Hook aber nicht.

### 6.4 WebSocket-Recordings

`tests/fixtures/recorded/ws/` (AP-04 K2/K4) folgt einem eigenen JSONL-
Format (`{ts, dir, topic, raw, parsed}`). Die WS-Subscribe-Frames
enthalten weder Bearer-Tokens (Auth läuft per Cookie aus dem
HTTP-Login) noch URL-safe-Token-ähnliche Strings — Header-Redaktion
greift dort nicht (es gibt keine Header in WS-Frames). Sensitive Felder
auf der WS-Seite sind der aktive Live-Account-Identifier (heute
`U25235077`; Cutover auf dediziertes Service-Konto geplant — siehe
[`docs/02-architecture.md`](02-architecture.md) Sektion 11.2 und
[`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md))
und `session_id` aus dem ersten Tickle.

---

## 7. 2FA-Lifecycle gegenüber IBKR

Volles Runbook: [`docs/runbooks/cpgateway-login.md`](runbooks/cpgateway-login.md).
Operationelle Trigger: [`docs/03-deployment.md`](03-deployment.md) Sektion 8.

### 7.1 Modell

- **Genau eine** IBKR-Trading-Session pro Konto, gehalten vom
  `cpgateway`-Container im Compose-Stack des Live-Hosts.
- Initialer Login ist eine Operator-Aufgabe: Browser-2FA gegen IBKR
  über SSH-Reverse-Tunnel zum nicht-publizierten cpgateway-Port 5000.
- Anschließend hält der `gateway`-Service (FastAPI) die Session per
  `POST /tickle` alle 60 s warm.

### 7.2 Wann ein Re-Login zwingend ist

| Trigger | Re-Login? |
|---|---|
| `docker compose restart cpgateway` | **Ja** |
| Image-Rebuild + `up -d cpgateway` | **Ja** |
| Reboot des Hosts | **Ja** |
| `gateway` neu gestartet, `cpgateway` läuft weiter | nein |
| `competing: true` (paralleler Login von der IBKR-Mobile-App) | **Ja**, sobald die andere Session beendet ist |
| Tickle scheitert > 3× in Folge | **Ja** — Service kippt auf `auth_lost` |

Vor dem 2FA-Login zuerst `POST /iserver/reauthenticate` versuchen, dann
Drift-Check 2× mit 90 s Warmup-Pause. Erst wenn das nicht hilft,
Browser-2FA. Voller Ablauf:
[`docs/runbooks/cpgateway-session-resume.md`](runbooks/cpgateway-session-resume.md).

### 7.3 Status-Endpunkte

| Endpoint | Auth | Aussage |
|---|---|---|
| `GET /v1/internal/health` | `admin:*` | `cp_reachable`, `session_status`, `last_tickle` |
| direkter Aufruf gegen `cpgateway:5000/v1/api/iserver/auth/status` | innerhalb des Compose-Netzes; vom Host per `docker exec` | `authenticated`, `competing`, `connected` |

Bei `auth_lost` antworten alle Business-Endpunkte mit `503` +
`Retry-After: 30`.

### 7.4 Auto-Login für Paper-Stack (Karte ece90a8e)

Seit v1.28.0 hat der Paper-Stack einen optionalen Auto-Login-Pfad,
der den Browser-2FA-Schritt für den Paper-Account `cborlm399`
ablöst. Wichtig für die Sicherheits-Bilanz:

**Hard-Guards (mehrschichtig, defensive depth):**

| Schicht | Mechanismus | Konsequenz bei Verletzung |
|---|---|---|
| App-Level (Hard-Guard 1) | `validate_runtime_config` beim Lifespan-Start | `ConfigError` → Lifespan bricht ab, Service kommt nicht hoch |
| App-Level (Hard-Guard 1b) | wie Hard-Guard 1, prüft zusätzlich auf `BG_PAPER_USERNAME`/`BG_PAPER_PASSWORD` im Live-Stack | wie oben |
| Trigger-Level | `AutoLoginTrigger.maybe_trigger` skipped bei `stack_kind != "paper"` | reason `stack_kind_live` ohne Sidecar-Aufruf |
| Sidecar-Level (Phase B) | `auto_login.py` prüft Ziel-URL gegen `paper-cpgateway` | Exit-Code 5 vor Form-Submit |
| Compose-Level (Hard-Guard 3) | Live-Compose hat keinen `docker.sock`-Mount, keine `BG_PAPER_*`-Vars | Trigger kann auf Live nichts starten, selbst wenn Code-Pfade umgangen würden |

**Credential-Lagerung:** `BG_PAPER_USERNAME` und `BG_PAPER_PASSWORD`
liegen ausschließlich auf dem Pi unter `/etc/default/broker-gateway-
paper` (Mode 0600 root:root) und werden vom Compose-Stack als
Environment in den `gateway`-Container hineingereicht. Der Sidecar
bekommt sie ebenfalls per Env (niemals als CLI-Argument, niemals in
einer Image-Layer). Die globale Header-/Body-Redaktion (Sektion 4+5)
filtert alle bekannten Credential-Strings; Phase B ergänzt zusätzlich
einen Username-Maskierer (`cborlm399` → `cb***99`) im Sidecar-eigenen
Logger.

**2FA-Failure-Mode:** Erkennt der Sidecar plötzlich ein 2FA-Element
(zusätzliches Eingabefeld, Redirect auf `/sso/2fa`), beendet er sich
mit Exit-Code 4. `AutoLoginTrigger` setzt den Throttle-Status auf
`2fa_required_manual_intervention` (sticky), und jeder weitere
Trigger-Aufruf wird gestoppt — bis ein Mensch die Service-Instanz neu
startet und den manuellen Browser-2FA-Login durchführt. Damit ist
ausgeschlossen, dass ein verändertes IBKR-Login-Verhalten unbemerkt
zu Lockout-Schaden führt.

**Docker-Socket-Risiko (Phase B):** Der Paper-Stack mountet
`/var/run/docker.sock` in den `gateway`-Container, damit der
Auto-Login-Sidecar via `docker run --rm` gestartet werden kann. Das
ist **bewusst akzeptiert**, weil im Paper-Stack kein Live-Geld läuft.
Mitigations:

- AppArmor- bzw. Seccomp-Profil auf dem Pi-Host beschränkt die
  zulässigen `docker`-Calls auf `run --rm broker-gateway-paper-auto-
  login*`. Falls AppArmor zu komplex zu betreiben ist, wird das
  Restrisiko explizit dokumentiert (hier).
- Live-Stack mountet den Socket **nie** — Hard-Guard 3.

**Konservative Throttle-Begründung:** IBKR publiziert keine
offiziellen Lockout-Schwellen. Forum-Berichte deuten auf ~3-5
fehlgeschlagene Logins → temp-Lockout 15 min, ~5-10 → Account-Sperre
mit Support-Ticket. Die Throttle-Defaults (max 1/5min, 3/h, 5/Tag,
Backoff 5/15/45min) bleiben deutlich unter diesen Werten und
vermeiden bot-typisches Hammering-Pattern.

---

## 8. Netzwerk-Sicherheit

### 8.1 Stand heute

| Schicht | Stand |
|---|---|
| Gateway-Port 4000 | extern auf cma-pi-1 publiziert; Erreichbarkeit nur über Tailscale-Mesh |
| `cpgateway`-Port 5000 | **nicht** extern publiziert; nur intern im Compose-Netz |
| `/metrics` | nur intern (kein extern publizierter Port — wird über lokalen Prometheus-Scraper auf demselben Host abgefragt) |
| TLS auf der externen Schicht | nicht aktiv (HTTP über Tailscale) |

Tailscale-Mesh ist die wirksame Authentifizierungs-Schicht für
Netzwerk-Erreichbarkeit. Wer `cma-pi-1:4000` aufrufen kann, hat eine
Tailscale-Identität — Bearer-Token ist die zweite Hürde.

### 8.2 Geplant / offen

- **Externer TLS-Endpunkt** (Caddy/Nginx + Let's Encrypt) ist **nicht**
  entschieden. Solange der Service nur über Tailscale erreichbar ist,
  fehlt der Anlass.
- **mTLS** zwischen Gateway und Consumern ist nicht implementiert (Bearer-
  Token reicht heute).
- **Rate-Limit** beim Eingang ist im Service als Token-Bucket pro
  Endpoint-Klasse umgesetzt (`throttle/`), aber nicht an einen
  Reverse-Proxy ausgelagert.

---

## 9. Idempotency-Sicherheit

Implementierung: [`src/broker_gateway/idempotency.py`](../src/broker_gateway/idempotency.py).

- TTL: Default 24 h, override per `BG_IDEMPOTENCY_TTL_S`.
- Storage: In-Memory-Map mit Thread-Lock.
- Verlust beim Service-Restart ist **akzeptiert** (transient).
  Konsequenz: Replay eines Schreibvorgangs nach Restart erzeugt einen
  echten zweiten Order-Auftrag, falls der Consumer denselben
  Idempotency-Key wiederholt.
- Mitigation: Consumer **muss** für jeden Logik-Schritt einen frischen
  Idempotency-Key generieren (UUIDv4 oder ähnlich), sodass ein
  Replay erst dann passiert, wenn der Consumer denselben Key absichtlich
  zweimal sendet.

Optional Redis als externes State-Backing für Restart-Persistenz ist im
Code-Pfad nicht aktiv (siehe Architektur-Sektion 11.2).

---

## 10. Reaktion auf Vorfall

### 10.1 Kompromittierter Bearer-Token

1. Token sofort revoken: `DELETE /v1/auth/token` mit Admin-Header oder
   Self-Auth des betroffenen Tokens.
2. Neuen Token erzeugen, Consumer-Konfiguration umpflegen.
3. Logs (`inbound.log`) auf Anomalien zwischen erstem Compromise-Verdacht
   und Revoke-Zeitpunkt prüfen — Filter auf `caller_id` des Tokens, nicht
   auf den Wert.
4. Falls `orders:write` involviert: über `/v1/trades` und
   `/v1/orders/{id}` prüfen, ob Orders auftauchten, die nicht vom
   Konsumenten stammen.

### 10.2 Kompromittiertes Recording

Eine bereits committed/gepushte Fixture mit Token-/Cookie-Werten:

1. Repo-History bereinigen (`git filter-branch` / BFG / Git-Repo-
   Rotation) — die Tatsache, dass die Datei „deleted" ist, reicht
   nicht; sie liegt weiter in der History und auf jedem Clone.
2. Den betroffenen Token (falls echt) revoken.
3. Den `cpgateway`-Login zurücksetzen, weil Cookies bei IBKR potenziell
   replay-fähig sind, bis die Session abgelaufen ist (Lifecycle-Pause
   90 s Warmup).
4. Recording neu aufnehmen mit Recorder-Filter aktiv und vor dem
   `git add` manuell prüfen.

### 10.3 Kompromittierter Host (cma-pi-1)

1. Service stoppen: `docker compose down`.
2. `BG_TOKEN_FILE` (falls genutzt) als gelöscht markieren — alle
   Tokens müssen als kompromittiert gelten.
3. cpgateway-Session muss als kompromittiert gelten — Browser-2FA-
   Reset auf dem aktiven Live-Konto (heute U25235077; Cutover auf
   dediziertes Service-Konto geplant — siehe
   [`docs/02-architecture.md`](02-architecture.md) Sektion 11.2 und
   [`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md))
   in Erwägung ziehen (Operator-Entscheidung).
4. Host neu aufsetzen, neuen Bootstrap-Token generieren, Consumer
   einzeln re-authenticaten.

Es gibt heute **kein** automatisches Audit-Trail jenseits der drei Logs —
forensische Rekonstruktion läuft über `inbound.log` (Body 1:1) und
`cp_wire.log` (sobald scharf, AP-05 K3).

---

## 11. Was bewusst NICHT in v1 abgedeckt

- **OAuth/OIDC-Flow.** Bearer-Tokens reichen für die heutigen Konsumenten;
  ein Identity-Provider-Pfad (Auth0, Keycloak) ist Overkill für den
  Singular-Service.
- **Signierte Tokens / JWT.** Opaque-Lookup-Modell ist bewusst gewählt —
  einfaches Revoke ohne Jot-Cache.
- **mTLS.** Tailscale-Mesh + Bearer-Token sind die authentifizierende
  Schicht.
- **Hardware-Key-2FA für Operator** (FIDO2/Yubikey gegen den IBKR-Login)
  bleibt eine IBKR-Konto-Setting-Frage, kein Service-Concern.
- **Audit-Log mit revisions-sicherer Speicherung.** Strukturierte Logs
  + File-Rotation reichen heute.
- **Verschlüsselung-at-rest für `tokens.json`.** Filesystem-Permissions
  sind Operator-Aufgabe; LUKS oder ähnliches wäre eine Host-Maßnahme.

---

## 12. Verweise und offene Fragen

### 12.1 Verweise

- Architektur Auth-Modell: [`docs/02-architecture.md`](02-architecture.md) Sektion 6
- Architektur Logging: [`docs/02-architecture.md`](02-architecture.md) Sektion 8
- Deploy-Doku: [`docs/03-deployment.md`](03-deployment.md)
- Login-Runbook: [`docs/runbooks/cpgateway-login.md`](runbooks/cpgateway-login.md)
- Recording-Konzept: [`docs/cp-recordings.md`](cp-recordings.md)
- Header-Redaktion-SSOT: [`src/broker_gateway/cp/redaction.py`](../src/broker_gateway/cp/redaction.py)
- Auth-Modul: [`src/broker_gateway/auth/`](../src/broker_gateway/auth/)
- Idempotency: [`src/broker_gateway/idempotency.py`](../src/broker_gateway/idempotency.py)

### 12.2 Offene Sicherheits-Fragen

- ~~**Pre-Commit-Hook für Recordings.**~~ Geklärt mit AP-05 K4
  (v1.13.0): Hook in `scripts/pre_commit_recording_scan.py`, aktiviert
  über `pre-commit install`, siehe Sektion 6.3.
- ~~**Body-Token-Scan.**~~ Geklärt mit AP-05 K5 (v1.14.0): Test
  `tests/test_no_token_leak_in_bodies.py` erzeugt einen Bearer-Token
  und prüft pro Read-Endpunkt, dass der Wert weder im Body noch in
  Response-Headern auftaucht. SSE-Stream-Endpunkte sind out-of-scope
  dieser Karte (separate Karte falls Token-Echo dort relevant wird).
  Siehe Sektion 4.3.
- ~~**Permissions auf `tokens.json`.**~~ Geklärt mit AP-10 K1 (v1.15.0):
  `FileTokenStore` warnt beim Init bei zu offenen Permissions und
  schreibt neue/aktualisierte Dateien per `os.chmod(..., 0o600)`. Siehe
  Sektion 2.2.
- **Audit-Log für Admin-Aktionen.** Token-Erzeugung und Revoke laufen
  heute durch dieselben Inbound-Logs wie alles andere. Ein dedizierter
  `audit.log`-Strang für Admin-Schreibaktionen ist denkbar.
- **TLS-Endpunkt.** Solange Tailscale-internal genügt, brauchen wir kein
  externes TLS — die Frage muss bei Bedarf erneut geprüft werden.
- **Konto-Trennung User vs. Service.** Hintergrund: jeder Browser-/App-
  Login des Operators auf U25235077 kollidiert mit der broker-gateway-
  Live-Session (Single-Session-Constraint
  [`02-architecture.md`](02-architecture.md) Sektion 2.1 / 3.1) und kostet
  Service-Verfügbarkeit + 2FA-Re-Login. Lösungsweg: dediziertes IBKR-
  Service-Konto ausschließlich für broker-gateway-Live, beantragt
  2026-05-18; U25235077 bleibt nach dem Cutover Operator-Privatkonto.
  Operator-Pfad und Phasenplan in
  [`docs/runbooks/account-cutover.md`](runbooks/account-cutover.md);
  Status in [`02-architecture.md`](02-architecture.md) Sektion 11.2
  ("Account-Identitaet-Wechsel").

---

*Stand: v2.1.4 (2026-05-16). Karten mit Auth-, Logging-, Recording-
oder Tokenmodell-Wirkung aktualisieren dieses Dokument oder verweisen
explizit auf die Sektion, die zu pflegen ist.*
