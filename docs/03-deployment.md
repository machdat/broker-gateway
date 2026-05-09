# 03 — Deployment

Lebende Anleitung, wie `broker-gateway` betrieben wird. Single Source of
Truth für: Deploy-Targets, Pfad-Konventionen, Compose-Layout,
Deploy-Workflow, Healthchecks, Restart-Disziplin, Tooling-Stolpersteine
und Rollback.

> Architektur-Hintergrund (warum zwei Container, warum Singular-Halter
> usw.) lebt in [`docs/02-architecture.md`](02-architecture.md).
> Login-Detail (Browser-2FA, SSH-Tunnel) lebt im Runbook
> [`docs/runbooks/cpgateway-login.md`](runbooks/cpgateway-login.md).

**Stand:** v2.0.0, 2026-05-09. **TWS-Backend ist Default seit Karte 5
Hard-Cutover.** Die Sektionen 3 (Compose-Layout) und 8 (2FA-Lifecycle)
unten beschreiben noch den frueheren cpgateway-Pfad — sie gelten
weiterhin als Roll-Back-Referenz. Der aktuelle Default-Stack
(`gnzsnz/ib-gateway:stable` + IBC + `ib_async`) ist im neuen
[Section 3a unten](#3a-aktueller-tws-stack-v200) zusammengefasst und
in `compose.yaml` direkt sichtbar.

## Inhalt

1. [Deploy-Targets](#1-deploy-targets)
2. [Pfad-Konventionen](#2-pfad-konventionen)
3. [Compose-Layout](#3-compose-layout)
3a. [Aktueller TWS-Stack (v2.0.0)](#3a-aktueller-tws-stack-v200)
4. [Deploy-Workflow](#4-deploy-workflow)
5. [Healthchecks und Verifikation](#5-healthchecks-und-verifikation)
6. [Restart-Disziplin](#6-restart-disziplin)
7. [Tooling-Hinweise](#7-tooling-hinweise)
8. [2FA-Lifecycle bei cpgateway](#8-2fa-lifecycle-bei-cpgateway)
9. [ENV-Variablen Live vs Paper](#9-env-variablen-live-vs-paper)
10. [Rollback](#10-rollback)
11. [Offene Fragen](#11-offene-fragen)

---

## 1. Deploy-Targets

| Stack | Host | Compose-Project-Name | Status |
|---|---|---|---|
| **Live (U25235077)** | `cma-pi-1` | `broker-gateway` (Default) | **deployed v2.0.0**, TWS-Backend, in Betrieb |
| **Paper (DUP799747)** | `cma-pi-1` | `broker-gateway-paper` | **deployed v2.0.0**, TWS-Backend, in Betrieb |

Live-Stack ist Single-Owner der IBKR-Trading-Session — siehe
Architektur-Dok Sektion 3.1. Es gibt **eine** Live-Instanz pro Konto.

Paper-Stack ist als zweite, parallel laufende Instanz mit IBKR-Paper-
Konto geplant. Der Code-Pfad ist identisch zum Live-Stack — der
einzige Unterschied liegt in `.env` (Paper-Account-ID `DU…`,
eigener Compose-Project-Name, eigene Volumes/Ports).

## 2. Pfad-Konventionen

| Stack | Repo-Pfad auf Host | Recordings | Logs | Notes |
|---|---|---|---|---|
| Live | `/mnt/ssd/broker-gateway` | `tests/fixtures/recorded/live/` (gitignored im Container, eingecheckt im Repo nur als Snapshot) | `var/cpgateway/logs/` (Volume) + `BG_LOG_DIR` falls gesetzt | SSD wegen Recording- und Log-Durchsatz |
| Paper (geplant) | `/mnt/ssd/broker-gateway-paper` (oder zweiter Host) | `var/recordings-paper/` als Bind-Mount nach `tests/fixtures/recorded/paper/` | analog mit eigenem Verzeichnis | siehe AP-06 |

`var/cpgateway/logs/` wird als Bind-Mount in den `cpgateway`-Container
gemountet. UID/GID des Container-Users `cpgw` muss zur Host-User-UID
passen (Default 1000:1000 = `cma:cma` auf cma-pi-1) — sonst gehören
Logs einem im Host nicht existierenden User. Override per Build-Args
`CPGW_UID`/`CPGW_GID` aus `.env`. Prüfen mit `id cma`.

## 3a. Aktueller TWS-Stack (v2.0.0)

Seit Karte 5 (Hard-Cutover, 2026-05-09) ist der TWS-Backend-Pfad der
Default-Stack:

```
gateway     : Image broker-gateway:2.0.0     extern 4000 (live) / 4001 (paper)
tws         : Image gnzsnz/ib-gateway:stable VNC 127.0.0.1:5906/5905 -> 5900
cpgateway   : Image broker-cpgateway:1.0.3   nur unter Profile cp-legacy
```

- `gateway` (FastAPI) `depends_on tws: condition: service_healthy`.
  `BG_BACKEND=tws`, `BG_TWS_HOST=tws`, `BG_TWS_PORT=4004` (paper) bzw.
  `4003` (live).
- `tws` (gnzsnz/ib-gateway:stable) startet IB Gateway 10.x + IBC + Xvfb
  headless. IBC fuehrt den Login durch und klickt die Configure-Settings
  (TWS_ACCEPT_INCOMING=accept, READ_ONLY_API=yes, BYPASS_WARNING=yes).
  socat-Forward von 4001/4002 auf 4003/4004 macht den TWS-Socket fuer
  den gateway-Container erreichbar.
- `cpgateway` ist nicht mehr Default-aktiv. Profile `cp-legacy` zieht
  ihn nur, wenn `ops/rollback-to-cp.sh --env={live,paper}` ausgefuehrt
  wird (Notfall-Pfad).

**Live-2FA:** Bei Container-Recreate muss der Operator via VNC die
2FA-Methode "IB" anwaehlen und am Handy zweimal die Push-Bestaetigung
geben. Details in der Auto-Memory `project_live_2fa_gnzsnz_pattern`.
Paper (cborlm399) hat kein 2FA und laeuft skriptbar durch.

**Cutover-Skripte:** `ops/cutover-tws.sh --env={live,paper}` (auf TWS)
und `ops/rollback-to-cp.sh --env={live,paper}` (zurueck auf cpgateway).

**Folgekarten:** Nach 30 Tagen stabiler TWS-Operations werden die
Code-/Compose-Reste fuer den cp-Pfad in einer eigenen Karte komplett
entfernt (`src/broker_gateway/cp/*`, `ops/cpgateway/`,
`Dockerfile.cpgateway`, `compose.cp-legacy.yaml`, `ops/auto-login/`).
Persistenz fuer `/home/ibgateway/Jts` (heute ephemeral) und
Order-Submission-Pfad (READ_ONLY_API=no) sind separate Karten.

## 3. Compose-Layout

> **Hinweis 2026-05-09:** Diese Sektion beschreibt das **frühere**
> cpgateway-Layout. Es gilt nur noch als Roll-Back-Referenz — der
> aktuelle Default-Stack ist in [3a](#3a-aktueller-tws-stack-v200)
> oben.

Stack besteht aus zwei Services in `compose.yaml`:

```
gateway     : Image broker-gateway:1.11.0       extern  4000  intern 8000
cpgateway   : Image broker-cpgateway:1.0.3      kein-extern (nur intern)  5000
```

- `gateway` (FastAPI / uvicorn) wartet via
  `depends_on: condition: service_healthy` auf `cpgateway`.
- `cpgateway` ist nicht extern publiziert. Externer Zugriff für den
  Browser-2FA-Login ausschließlich über SSH-Reverse-Tunnel.
- Compose-Healthcheck am `cpgateway` akzeptiert HTTP 401 als „lebendig,
  aber unauth" — das ist der Normalzustand vor dem Login.
- Beim Image-Bump `image:`-Tag in `compose.yaml` mitziehen
  (Konvention: Image-Tag = Service-Version).

Konfiguration des CP-Gateways selbst (listenPort, listenSsl=false) liegt
in [`ops/cpgateway/conf.yaml`](../ops/cpgateway/conf.yaml). Tarball
`clientportal.gw.tar.gz` wird **nicht** versioniert — nur die
SHA256-Prüfsumme in [`ops/cpgateway/clientportal.gw.tar.gz.sha256`](../ops/cpgateway/clientportal.gw.tar.gz.sha256)
ist eingecheckt; siehe [`ops/cpgateway/README.md`](../ops/cpgateway/README.md)
für den Bezug.

## 4. Deploy-Workflow

### 4.1 Aktueller Stand: Direct-to-main

CI ist noch nicht aufgesetzt. Bis dahin gilt die Übergangsregel aus
`CLAUDE.md`: **direkt auf `main` committen**, kein PR-Zwang. Sobald
CI steht, wird auf Branch + PR umgestellt.

Schritte für eine funktionale Änderung:

```bash
# 1. lokal
pytest                          # gruene Suite
git add <files>
git commit                      # Version-Bump in pyproject.toml + README mitcommitten
git push origin main            # autonom, sobald Karte freigegeben (Memory-Regel)

# 2. auf cma-pi-1
ssh cma@cma-pi-1
cd /mnt/ssd/broker-gateway
git pull
./ops/build-gateway.sh          # build + drift-acceptance + up -d gateway
docker compose ps gateway       # Healthcheck-Status pruefen
```

`ops/build-gateway.sh` (siehe [`ops/build-gateway.sh`](../ops/build-gateway.sh))
fasst die drei Schritte zusammen:

1. `docker compose build gateway`
2. `scripts/check_mock_drift.py --build-acceptance` — Build bricht ab,
   wenn Mock-Fixture und Live-Antwort divergieren.
3. `docker compose up -d gateway`

**Notfall-Bypass:** `SKIP_ACCEPTANCE=1 ./ops/build-gateway.sh` —
nur für begründete Doku-only-Notfälle.

### 4.2 Doku-only-Karten

Karten ohne Code-Wirkung haben `deployment_required=false`. Sie werden
gepusht, aber **kein** `ops/build-gateway.sh` läuft, und der Service
wird nicht neu gestartet. Beispiele: AP-09-Karten (Architektur-/Deploy-
/Security-/API-/Glossar-Doku), Skill-Updates, Skript-only-Erweiterungen
unter `scripts/` ohne Service-Bindung.

### 4.3 cpgateway-Image-Update

Wenn der `cpgateway`-Tarball oder das `Dockerfile.cpgateway` ändert
(z.B. neuer IBKR-Tarball):

```bash
ssh cma@cma-pi-1
cd /mnt/ssd/broker-gateway
git pull
docker compose build cpgateway
docker compose up -d cpgateway
# Browser-Login erforderlich (Container-Restart kicked die Session)
```

Anschließend Login-Runbook: [`docs/runbooks/cpgateway-login.md`](runbooks/cpgateway-login.md).

### 4.4 systemd-Units (Drift-Detection)

Die systemd-Units in `ops/systemd/` (`doc-drift.timer`,
`doc-drift.service`) werden **nicht** automatisch deployed — die
Installation auf cma-pi-1 ist eine bewusste Aktion mit echtem
KanPrompt-API-Key. Anleitung: [`ops/systemd/README.md`](../ops/systemd/README.md).

## 5. Healthchecks und Verifikation

| Endpoint | Zweck | Erwartung |
|---|---|---|
| `GET http://localhost:4000/v1/health` | Liveness, kein Auth | `{"status":"ok","version":"1.11.0"}` |
| `GET http://localhost:4000/v1/internal/health` (admin-Bearer) | IBKR-Auth-Status, Tickle-Age, Subscription-Count | `cp_reachable: true`, `session_status: "ok"`, `last_tickle` < 60 s alt |
| `GET http://localhost:4000/metrics` | Prometheus-Scrape | Metriken-Bytes |
| `docker compose ps gateway` | Compose-Healthcheck | `healthy` |
| `docker compose ps cpgateway` | Compose-Healthcheck | `healthy` (HTTP 401 zählt als lebendig) |

Verifikation nach Deploy:

```bash
# auf cma-pi-1
docker compose ps
curl -s http://localhost:4000/v1/health | jq .
curl -s -H "Authorization: Bearer $BG_BOOTSTRAP_ADMIN_TOKEN" \
     http://localhost:4000/v1/internal/health | jq .
```

Bei `auth_lost` antwortet jeder Business-Endpunkt mit `503` +
`Retry-After: 30`. Recovery-Job versucht bis zu 3× `reauthenticate`,
sonst muss manuell der Login-Runbook durchgespielt werden.

## 6. Restart-Disziplin

| Anlass | `gateway`-Restart? | `cpgateway`-Restart? | Login? |
|---|---|---|---|
| Code-Änderung in `src/` | **Ja** (über `ops/build-gateway.sh`) | nein | nein |
| `pyproject.toml`-Bump (deps) | **Ja** | nein | nein |
| `compose.yaml`-Image-Tag-Bump | **Ja** | nein | nein |
| `Dockerfile.cpgateway` oder Tarball ändert | nein | **Ja** | **Ja** |
| Doku-only (`docs/`, `README.md`, `CLAUDE.md`, `CHANGELOG.md`) | nein | nein | nein |
| Skript-only (`scripts/`, `ops/systemd/`) ohne Service-Bindung | nein | nein | nein |
| ENV-Variable in `.env` ändert | **Ja** (compose-restart genügt) | nein, sofern `cpgateway`-Werte unverändert | nein |
| Host-Reboot (cma-pi-1) | automatisch (`restart: unless-stopped`) | automatisch | **Ja** |

Karten setzen das `deployment_required`-Flag in KanPrompt entsprechend:
- `true` (Default bei Code-Änderungen) → Pi-Deploy nötig.
- `false` (Doku/Skill/Skript-only) → kein Restart.

## 7. Tooling-Hinweise

### 7.1 Bash-Hook blockiert cma-pi-1 direkt

Auf Windows-Hosts (Claude-Code-Setup auf dem Laptop) blockiert ein
SessionStart-Hook direkte Befehle gegen `cma-pi-1`. Workaround: alle
SSH-Calls über **PowerShell** statt Bash absetzen oder mit
`! ssh …`-Prefix vom User-Prompt aus, damit der Hook nicht greift.

Praktisch heißt das: `ssh cma@cma-pi-1 'docker compose …'` aus einem
PowerShell-Fenster oder via Run-in-Background-Patterns. Die
Auto-Memory-Notiz `feedback_bash_hook_cma_pi_1` ist die Quelle für
diesen Workaround.

### 7.2 docker exec gegen Browser-Login

Auf dem Host kein Bedarf für SSH-Tunnel — direkte `curl`-Calls gegen
`http://localhost:5000/v1/api/...` reichen. Vom Laptop aus geht das
nur über den SSH-Reverse-Tunnel (Login-Runbook).

### 7.3 Drift-Check außerhalb des Builds

Manueller Probelauf von `scripts/check_mock_drift.py` ohne Acceptance-
Gate:

```bash
python scripts/check_mock_drift.py --base-url http://localhost:5000/v1/api
```

Bedingt warme IBKR-Session (`authenticated: true`).

## 8. 2FA-Lifecycle bei cpgateway

Volle Anleitung im Login-Runbook: [`docs/runbooks/cpgateway-login.md`](runbooks/cpgateway-login.md).

Zusammenfassung der Trigger, die einen Re-Login erzwingen:

| Anlass | Re-Login? |
|---|---|
| `docker compose restart cpgateway` | **Ja** — neue Session, kein gespeichertes Cookie |
| Image-Rebuild + `up -d cpgateway` | **Ja** |
| Reboot des Hosts | **Ja** |
| `gateway`-Service neu gestartet, `cpgateway` läuft weiter | nein |
| `competing: true` im `/iserver/auth/status` (paralleler Login von Mobile-App) | **Ja**, sobald die andere Session beendet ist |
| Tickle scheitert > 3× in Folge | **Ja** — Service kippt auf `auth_lost` |
| `gateway` antwortet `503` mit `Retry-After`, `auth_lost` im Internal-Health | **Ja** |

**Reauth nach Pause** ist als eigenes Runbook konsolidiert:
[`docs/runbooks/cpgateway-session-resume.md`](runbooks/cpgateway-session-resume.md).
Kurz: erst `POST /iserver/reauthenticate`, dann Drift-Check 2× mit
90 s Warmup-Pause; Browser-2FA erst bei zweimaligem Misserfolg.

Troubleshooting bei sechs typischen Fehlerbildern:
[`docs/runbooks/cpgateway-troubleshooting.md`](runbooks/cpgateway-troubleshooting.md).

## 9. ENV-Variablen Live vs Paper

`.env.example` (eingecheckt) ist Vorlage — `.env` selbst liegt nur
lokal und ist gitignored. Anlegen auf neuem Host:

```bash
cp .env.example .env
python3 -c 'import secrets; print("BG_BOOTSTRAP_ADMIN_TOKEN=" + secrets.token_urlsafe(32))' >> .env
sed -i '/^BG_BOOTSTRAP_ADMIN_TOKEN=$/d' .env
```

| Variable | Default | Live | Paper (geplant) |
|---|---|---|---|
| `BG_STACK_KIND` | (Pflicht, kein Default) | `live` | `paper` |
| `BG_BOOTSTRAP_ADMIN_TOKEN` | leer | Pflicht (32-Byte URL-safe) | Pflicht, eigener Wert |
| `BG_CP_BASE_URL` | `http://cpgateway:5000` | Default OK | `http://cpgateway-paper:5001` (geplant) |
| `BG_CP_TICKLE_INTERVAL_S` | `60` | Default | Default |
| `CPGW_UID` / `CPGW_GID` | `1000` | Default für `cma`-User | analog |
| `BG_TOKEN_FILE` | leer (Memory) | optional, z.B. `/var/lib/broker-gateway/tokens.json` | analog mit eigenem Pfad |
| `BG_LOG_DIR` | leer (stdout) | gesetzt für File-Sinks | analog |
| `BG_LOG_LEVEL`, `BG_LOG_ROTATE_*`, `BG_LOG_INBOUND_BODIES` | siehe README | optional | optional |
| `BG_CP_RECORD_DIR` | leer | leer im Default; setzen für Recording-Sessions | gesetzt fest auf `var/recordings-paper/` (geplant AP-06) |
| `BG_PAPER_TESTS_DISABLED` | `false` | n/a (Hard-Guard 1 verbietet Paper-Vars im Live-Stack) | Kill-Switch für Paper-Test-Suiten |
| `BG_PAPER_AUTO_LOGIN` | `0` | **muss leer / `0` sein** (Hard-Guard 1 → Startup-Fail) | `0` (Default) oder `1` (Phase B aktivieren) |
| `BG_PAPER_USERNAME` | leer | **muss leer sein** (Hard-Guard 1b) | Paper-Account, z.B. `cborlm399` |
| `BG_PAPER_PASSWORD` | leer | **muss leer sein** | Paper-Passwort |

`BG_STACK_KIND` ist **Pflicht** seit v1.28.0. Fehlt der Wert oder
enthält er etwas anderes als `live`/`paper`, bricht der Lifespan-Start
mit `ConfigError` ab. `ops/build-gateway.sh` exportiert ihn defensiv
abhängig vom `--env=`-Schalter, sodass auch eine `.env`-Datei ohne
expliziten `BG_STACK_KIND`-Eintrag funktioniert.

Compose-Project-Name unterscheidet die Stacks im selben Host-Docker-
Daemon. Paper-Plan: `COMPOSE_PROJECT_NAME=broker-gateway-paper` in der
Paper-`.env`, dann `docker compose --env-file .env up -d` schreibt
einen separaten Container-Namespace.

### 9.1 Auto-Login-Credentials auf cma-pi-1

**Pfad:** `/etc/default/broker-gateway-paper` (Mode `0600`, root:root).

```bash
# Auf dem Pi, einmalig nach v1.28.0-Deploy:
sudo install -m 0600 -o root -g root /dev/null /etc/default/broker-gateway-paper
sudo tee /etc/default/broker-gateway-paper >/dev/null <<'EOF'
BG_PAPER_USERNAME=cborlm399
BG_PAPER_PASSWORD=<aus passwort-manager>
BG_PAPER_AUTO_LOGIN=0
EOF
```

`BG_PAPER_AUTO_LOGIN=0` als Default lässt das Trigger-Skeleton im
no-op-Modus — Phase B aktiviert es per Update auf `1` zusammen mit
dem Sidecar-Image-Build. Das build-gateway.sh-Skript (Phase B) liest
diese Datei und reicht die Werte als Env-File an
`docker compose up gateway` weiter; im Live-Stack wird die Datei
nicht eingelesen (Hard-Guard 3).

## 10. Rollback

### 10.1 Code-Rollback

```bash
ssh cma@cma-pi-1
cd /mnt/ssd/broker-gateway
git log --oneline -5
git reset --hard <vorheriger-Commit>
./ops/build-gateway.sh
```

`git reset --hard` ist akzeptabel auf cma-pi-1, weil dort keine
parallelen Branches gehalten werden. Lokal **niemals** ohne
ausdrückliche Bestätigung des Users — Datenverlust-Risiko bei lokaler
Arbeit.

### 10.2 Image-Rollback (kein git-Revert nötig)

Wenn das Vor-Image noch im lokalen Docker-Cache liegt:

```bash
docker compose down gateway
docker tag broker-gateway:<previous> broker-gateway:1.11.0   # oder image:-Tag in compose.yaml temporaer aendern
docker compose up -d gateway
```

### 10.3 cpgateway-Tarball-Rollback

Vorhergehende Tarball-Version wieder einchecken (oder lokal vorhalten),
SHA256 in `ops/cpgateway/clientportal.gw.tar.gz.sha256` zurückrollen,
dann `docker compose build cpgateway` + Login-Runbook.

### 10.4 Recordings-Vorhalte

`tests/fixtures/recorded/live/` ist eingecheckt und enthält die
kanonischen Mock-Fixtures (Sektion 9.2 in `02-architecture.md`).
Drift-Detection (Sektion 9.3 dort) erkennt, wenn Live-Antwort und
Fixture divergieren — das ist Auslöser für ein gezieltes
Re-Recording, nicht für ein Rollback.

## 11. Offene Fragen

- **Paper-Stack-Host:** AP-06 Karte 1 muss klären, ob der Paper-Stack
  auf cma-pi-1 koexistiert (zweiter Compose-Project-Name + Port 4001/5001)
  oder auf einem zweiten Host läuft.
- **CI-Übergang:** Direct-to-main bleibt aktuell. Sobald CI/CD steht
  (GitHub Actions oder lokal auf cma-pi-1), wird auf Branch + PR
  umgestellt — diese Doku ist dann zu aktualisieren.
- **Externer TLS-Endpunkt:** intern HTTP über Tailscale ist Stand;
  ein externer öffentlicher Endpunkt mit TLS (Caddy / Nginx) ist
  nicht entschieden.
- **Token-Datei-Pfad:** `BG_TOKEN_FILE` für Persistenz-Pfad ist im
  Code optional; ein verbindliches Default-Layout für cma-pi-1 ist
  nicht festgelegt (Memory in `tokens.json` ist heute der IST-Stand).

---

*Lebt mit dem Service. Karten mit Deploy-Wirkung aktualisieren dieses
Dokument oder verweisen explizit auf eine Sektion, die zu pflegen ist.*
