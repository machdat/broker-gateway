# Runbook: Token-Store-Verlust nach `docker compose up --force-recreate`

Wiederherstellung der Konsumenten-Zugriffe (PSM, trading_robot, ad-hoc CLI-Tokens),
nachdem ein Container-Recreate von `broker-gateway` den persistierten Token-Store
**doch** geleert hat oder vor v2.1.4 noch keinen FileTokenStore besass.

> **Wann ist dieser Pfad richtig?** Wenn nach einem Service-Recreate alle vorher
> ausgestellten Konsumenten-Tokens mit HTTP 401 antworten, obwohl die Konsumenten
> ihr `BROKER_GATEWAY_TOKEN` nicht geaendert haben. Vorher: pruefen, ob es nicht
> in Wahrheit ein IBKR-Session-Verlust ist
> ([`cpgateway-session-resume.md`](cpgateway-session-resume.md), Memory
> `project_container_recreate_kills_session`) — beide Vorfaelle treffen den
> Operator nach demselben Trigger und sehen oberflaechlich aehnlich aus.

## 1. Anlass-Erkennung

| Trigger | Wo sichtbar |
|---------|-------------|
| `docker compose up -d --force-recreate` lief (Image-Update, Env-Aenderung, manueller Recreate) | `docker logs --since 5m broker-gateway`/`broker-gateway-paper` zeigt frischen Lifespan-Start. |
| Konsumenten-Clients liefern HTTP 401 mit `WWW-Authenticate: Bearer realm="broker-gateway"` | PSM-Logs, trading_robot-Logs, eigene Curl-Calls. |
| `GET /v1/health` antwortet weiterhin HTTP 200 (kein IBKR-Problem) | `curl http://localhost:4000/v1/health`. |
| `GET /v1/internal/health` mit dem **Admin-Bootstrap-Token** liefert `auth_status: "ok"` | Bestaetigt: IBKR-Session lebt, Problem ist der Konsumenten-Token-Store. |

Wenn `/v1/internal/health` mit Admin-Token 200 antwortet aber Konsumenten 401
sehen, ist es **diese** Karten-Situation und nicht
[`cpgateway-session-resume.md`](cpgateway-session-resume.md).

### Warum tritt das auf?

- **Vor v2.1.4:** Der Service nutzte `InMemoryTokenStore` als Default — jeder
  Container-Recreate loescht alle Konsumenten-Tokens. Nur der initial via
  `BG_BOOTSTRAP_ADMIN_TOKEN` registrierte Admin-Token kommt beim naechsten
  Lifespan-Start neu zurueck.
- **Ab v2.1.4 (Karte `b05206c7`):** Default ist `FileTokenStore`, persistiert
  in `/var/lib/broker-gateway/tokens.json`. Compose mountet das ueber
  `BG_TOKEN_DIR_HOST:./var/broker-gateway` (Default) bzw.
  `/var/lib/broker-gateway` (Pi-Override). Der Token-Store ueberlebt
  Container-Recreates — **es sei denn**, das Volume wurde geloescht, der
  Mount-Pfad weicht ab oder die Datei wurde manuell entfernt.

## 2. Schritt 1 - Token-Store auf dem Pi pruefen

```bash
ssh cma@cma-pi-1
ls -la /var/lib/broker-gateway/tokens.json 2>/dev/null || \
    echo "keine Datei -- Token-Store leer"
sudo -n cat /var/lib/broker-gateway/tokens.json 2>/dev/null | \
    python3 -c 'import json,sys; d=json.load(sys.stdin); \
                print("\n".join(f"{t[\"caller_id\"]}: {t[\"scopes\"]}" for t in d["tokens"]))'
```

Erwartungen:

| Befund | Bedeutung |
|--------|-----------|
| Datei existiert, nur `bootstrap-admin` ist drin | Service ist v2.1.4+, aber der vorhandene Konsumenten-Datensatz wurde geloescht/nicht persistiert (z.B. Volume neu angelegt). Konsumenten neu provisionieren (Schritt 3). |
| Datei existiert, Konsument ist drin | Token-Store ist intakt. Pruefe in der Datei den `token_hash` und vergleiche mit dem Konsumenten-`.env`. Liegt ein anderer Token-Wert im Konsumenten-`.env`, hat eine Token-Rotation stattgefunden und das `.env` ist veraltet — Schritt 3 ausfuehren. |
| Datei fehlt vollstaendig | Service laeuft im InMemory-Modus (v2.1.4 ohne `BG_TOKEN_FILE`-ENV oder pre-v2.1.4). Alle Konsumenten-Tokens neu provisionieren (Schritt 3). Optional: Persistenz aktivieren (Schritt 4). |

> **Sicherheit:** `tokens.json` ist Mode `0600`, root-owned. `sudo -n cat` nur
> auf der Operator-Sitzung verwenden. Der `token_hash`-Wert ist nur ein BLAKE2b-
> Hash — der Klartext-Token kann nicht zurueckgewonnen werden. Wer den Token-
> Wert verloren hat, kann nur rotieren, nicht wiederherstellen.

## 3. Schritt 2 - Konsumenten-Tokens neu provisionieren

Voraussetzung: das `BG_BOOTSTRAP_ADMIN_TOKEN` aus
`/etc/default/broker-gateway` (live) bzw. `/etc/default/broker-gateway-paper`
(paper) liegt in einer ENV-Variable `$ADMIN_TOKEN`.

```bash
ssh cma@cma-pi-1
ADMIN_TOKEN=$(sudo grep ^BG_BOOTSTRAP_ADMIN_TOKEN /etc/default/broker-gateway-paper | cut -d= -f2)
BG_URL=http://localhost:4001   # paper; live = :4000
```

### 3a. Direkter `curl`-Pfad (universell, kein Konsumenten-Skript noetig)

```bash
curl -sS -X POST "${BG_URL}/v1/auth/token" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"caller_id":"<consumer-name>","scopes":["instruments:read","quotes:read"]}'
```

Response liefert `value` (Klartext-Token, nur einmal sichtbar) und die
hinterlegten `scopes`. Der `value` wandert in das `.env` des Konsumenten —
**nie** in dieses Runbook, **nie** ins Git, **nie** in den Karten-Log.

### 3b. Konsumenten-spezifische Rotations-Skripte

Die Konsumenten betreuen ihre eigenen Token-Rotations-Helfer. Sie nehmen
dem Operator den `curl`-Loop ab, schreiben das `.env` atomar und drehen
parallel auch das Konsumenten-Repo-`.env` auf dem Pi:

| Konsument | Skript | Speicherort |
|-----------|--------|-------------|
| `trading_robot` (Bot) | `scripts/issue-bot-token.sh --profile {paper,live}` | im **trading_robot-Repo** auf cma-pi-1 (`/mnt/ssd/trading_robot/scripts/`) |
| PSM | derzeit ueber direkten `curl`-Pfad (siehe 3a); ein Helfer ist als PSM-Folge-Karte geplant. | – |

> Das `issue-bot-token.sh`-Skript lebt **nicht** im broker-gateway-Repo, weil
> es das Konsumenten-eigene `.env` schreibt und Konsumenten-Scope-Defaults
> kennt. broker-gateway selbst hat bewusst keinen Rotations-Helfer — jeder
> Konsument haelt seinen Pfad.

## 4. Schritt 3 - Konsumenten-Container neu erzeugen

`.env`-Dateien werden nur beim Container-**Start** gelesen, nicht beim
`restart`. Nach dem Token-Update zwingend der Recreate:

```bash
# Beispiel trading_robot
ssh cma@cma-pi-1 'cd /mnt/ssd/trading_robot && docker compose up -d --force-recreate bot'

# Beispiel PSM (sofern als Container deployed)
ssh cma@cma-pi-1 'cd /mnt/ssd/personal_stock_manager && docker compose up -d --force-recreate <service>'
```

## 5. Schritt 4 - Smoke-Test gegen den neuen Token

```bash
NEW_TOKEN=<value-aus-3a-oder-im-konsumenten-env>
BG_URL=http://localhost:4001  # paper

# 1. Portfolio (sofern Konsument portfolio:read besitzt)
curl -sS -o /dev/null -w '%{http_code}\n' \
    -H "Authorization: Bearer ${NEW_TOKEN}" \
    "${BG_URL}/v1/portfolio/<account-id>"
# Erwartet: 200

# 2. Instruments (jeder Konsument hat instruments:read)
curl -sS -o /dev/null -w '%{http_code}\n' \
    -H "Authorization: Bearer ${NEW_TOKEN}" \
    "${BG_URL}/v1/instruments/search?symbol=AAPL"
# Erwartet: 200

# 3. Optionaler SSE-Smoke (Konsument mit orders:read oder orders:write)
curl -sS -N --max-time 3 -w '\nstatus=%{http_code}\n' \
    -H "Authorization: Bearer ${NEW_TOKEN}" \
    "${BG_URL}/v1/orders/stream?account=<account-id>"
# Erwartet: status=200 (curl-Timeout 28 ist OK — Stream bleibt offen).
# Hinweis: /v1/events/stream gibt es seit Service v2.14.0 nicht mehr
# (Karte 37fca2f3) - Order-Events laufen ueber /v1/orders/stream.
```

Liefern (1) und (2) HTTP 200, ist die Token-Rotation erfolgreich. (3) ist
diagnostisch — SSE-Timeout ist erwartet und keine Fehlerquelle (Memory
`feedback_sse_reconnect_pattern`; trading_robot `issue-bot-token.sh` hat
den Sonderfall seit Karte `0bbcecf0` korrekt modelliert).

## 6. Persistenz aktivieren (langfristige Loesung, Schritt 4 optional)

Wenn die Pruefung in Schritt 1 ergab, dass die Persistenz-Datei fehlt,
liegt eine alte Compose-Konfiguration ohne `BG_TOKEN_FILE` oder ohne
Volume-Mount vor. Erforderliche Bestandteile (Stand v2.1.4):

```yaml
services:
  gateway:
    environment:
      - BG_TOKEN_FILE=${BG_TOKEN_FILE:-/var/lib/broker-gateway/tokens.json}
    volumes:
      - ${BG_TOKEN_DIR_HOST:-./var/broker-gateway}:/var/lib/broker-gateway
```

Auf cma-pi-1 ist `BG_TOKEN_DIR_HOST=/var/lib/broker-gateway` in
`/etc/default/broker-gateway` bzw. `/etc/default/broker-gateway-paper`
gesetzt (Mode `0700`, root-owned). Ohne diese ENV greift der Compose-
Default `./var/broker-gateway` im Stack-Repo — was ebenfalls persistent
ist, aber **nicht** zwischen Live- und Paper-Stack geteilt wird.

## 7. Wann der Recreate-Pfad **nicht** ausreicht

| Symptom | Diagnose | Aktion |
|---------|----------|--------|
| Konsumenten 401 und `/v1/internal/health` zeigt `auth_status: "down"` | IBKR-Session ist ebenfalls verloren (typisch nach Container-Recreate, Memory `project_container_recreate_kills_session`). | Zuerst Session-Recovery: bei TWS-Backend `docker compose up -d --force-recreate tws` + 2FA, bei cp-Backend [`cpgateway-session-resume.md`](cpgateway-session-resume.md). Erst danach Token-Rotation. |
| `tokens.json` existiert mit Konsumenten-Hash, aber Konsument liefert trotzdem 401 | `.env` im Konsumenten-Container ist veraltet (anderer Token-Wert) oder Konsumenten-Container wurde nach Token-Rotation nicht recreated. | Schritt 2 + Schritt 3 erneut, `docker exec <consumer> env \| grep TOKEN` zur Verifikation. |
| Mehrere Konsumenten 401, Pi-Token-File ungeoeffnet | Volume-Mount-Pfad weicht von der `BG_TOKEN_FILE`-ENV ab (klassisch nach manueller Compose-Aenderung). | `docker compose config \| grep BG_TOKEN_FILE` + `docker compose config \| grep -A2 volumes:` vergleichen, ENV korrigieren, Service recreaten. |
| Auch der Admin-Token (`BG_BOOTSTRAP_ADMIN_TOKEN`) wird mit 401 abgelehnt | Wert im `.env` weicht vom Wert in `/etc/default/broker-gateway*` ab. | `.env`-Override pruefen; broker-gateway haelt ohne den Bootstrap-Wert keine Admin-Tokens. |

## 8. Bezug zu anderen Komponenten

- **Persistenz-Karte:** `b05206c7` (FileTokenStore Compose-Default,
  v2.1.4). Vor dieser Karte war das Problem unvermeidbar; danach ist
  es ein Operator-Konfigurationsfehler oder eine bewusste Volume-
  Loeschung.
- **Konsumenten-Skript:** `trading_robot/scripts/issue-bot-token.sh`
  rotiert Bot-Tokens, schreibt das `.env` atomar (`mv` + `chmod 600`)
  und fuehrt drei Smoke-Curls aus. Die SSE-Sonderfall-Behandlung wurde
  in `trading_robot`-Karte `0bbcecf0` korrigiert.
- **Sicherheits-Modell:** `docs/04-security.md` Sektion 2.2 beschreibt
  den `FileTokenStore`-Vertrag (`bootstrap-admin` automatisch wieder
  registriert, Klartext-Token im Hash nicht rueckrechenbar).
- **Verwandte Operator-Pfade:** `auto-login-paper-setup.md` (Paper-
  Auto-Login-Sidecar — anderer Lifecycle, gleiche Bedingung
  `docker compose up -d --force-recreate` als Trigger),
  `paper-account-setup.md` (Stack-Setup), Memory
  `project_container_recreate_kills_session` (Session-seitiges
  Pendant).
