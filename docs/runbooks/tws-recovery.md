# Runbook: tws-Stack-Recovery + Watchdog

**Karte:** `53c10ff4` · **Bezug:** v2.5.3 (Socket-Reconnect `6dbf3026`),
v2.5.5 (Listener-Healthcheck `c7daadee`)

Dieses Runbook deckt den Fall ab, dass ein tws-Stack
(`broker-gateway-tws` live bzw. `broker-gateway-paper-tws`) nicht mehr
mit IBKR verbunden ist, und beschreibt den automatischen Watchdog.

## 1. Symptom & Diagnose

Leitsymptom: `GET /v1/internal/health` zeigt `auth_status=tws_down`
bzw. `GET /v1/internal/tws-health` zeigt `connected=false`, oft mit
stetig steigenden `consecutive_reauth_failures`.

```bash
# Container-Health (Token-frei, primaeres Signal seit v2.5.5):
docker inspect -f '{{.State.Health.Status}}' broker-gateway-tws
#   healthy   -> IB-Gateway-API-Listener lebt
#   unhealthy -> Listener tot  = Vorfall-Zustand
#   starting  -> faehrt gerade hoch (kurz abwarten)

# gateway-Sicht (Token aus dem Container):
TOK=$(docker exec broker-gateway printenv BG_BOOTSTRAP_ADMIN_TOKEN)
curl -s -H "Authorization: Bearer $TOK" http://localhost:4000/v1/internal/tws-health
```

### Zwei Fehlerklassen unterscheiden

| Zustand | Ursache | Selbstheilung |
|---------|---------|---------------|
| Socket abgerissen, Listener **lebt** | TWS-Neustart, Netz-Blip | ✅ ib_async-Reconnect (v2.5.3) heilt autonom |
| IB-Gateway-Java-Prozess **tot** (Listener weg) | Crash/OOM/IBKR-Kick/Saturday-Reset | ❌ Reconnect laeuft ins Leere (kein Listener) → **force-recreate noetig** |

Der zweite Fall ist der Vorfall vom 22.–28.06.2026: ~6 Tage Live unbemerkt
down, weil der Listener-Prozess gestorben war und es weder Auto-Recreate
noch Alarm gab.

## 2. Manueller Recovery

### Paper (kein 2FA – skriptbar)

```bash
# recreate-tws.sh wechselt selbst ins Paper-Repo (BG_PAPER_REPO_DIR,
# Default /mnt/ssd/broker-gateway-paper) - von ueberall aufrufbar.
/mnt/ssd/broker-gateway/ops/recreate-tws.sh paper
# wartet NICHT auf den Healthcheck-Hochlauf; danach pruefen:
watch -n5 "docker inspect -f '{{.State.Health.Status}}' broker-gateway-paper-tws"
```

### Live (Mobile-2FA – Operator zwingend)

Der Live-Stack laeuft am Service-Konto mit Mobile-2FA; ein force-recreate
loest einen 2FA-Push aus, den **nur der Operator am Handy** bestaetigen
kann. Deshalb gibt es bewusst **keinen** automatischen Live-Recovery.

```bash
cd /mnt/ssd/broker-gateway
COMPOSE_PROJECT_NAME=broker-gateway docker compose --env-file .env \
  -f compose.yaml up -d --force-recreate tws
# -> 2FA-Push am Handy bestaetigen (TWOFA_DEVICE=IB Key, IBC klickt OK selbst)
# danach connected=true verifizieren (siehe Diagnose oben).
```

> **Genau EIN force-recreate.** Mehrfache schnelle Recreates erzeugen
> konkurrierende 2FA-Pushes, die sich gegenseitig entwerten. Nach dem
> Recreate ~60–90 s auf den 2FA-Push warten, bevor erneut versucht wird.

## 3. Watchdog (automatisch, seit Karte `53c10ff4`)

Ein systemd-Timer (`tws-watchdog.timer`, alle 15 min) ruft
`scripts/tws_watchdog.py`. Pro Stack werden zwei Token-freie Signale
geprueft (Docker-Health + `GET /v1/health`). Bei **dauerhaftem** Down
(>= `BG_WATCHDOG_DOWN_THRESHOLD` konsekutive Laeufe, Default 2 → ~30 min):

- **ntfy-Push** auf das konfigurierte Topic (aktiver Handy-Alarm),
  Re-Alarm fruehestens nach `BG_WATCHDOG_REALERT_HOURS` (Default 6 h).
- **Paper:** `ops/recreate-tws.sh paper` heilt automatisch (einmal pro
  Down-Episode; ein gescheiterter Versuch wird im naechsten Lauf erneut
  probiert).
- **Live:** nur Alarm (2FA-Constraint) → manueller Recovery nach Schritt 2.
- **Recovery:** wird ein zuvor alarmierter Stack wieder healthy, kommt ein
  „WIEDER OK"-Push und der Zustand wird zurueckgesetzt.

State liegt in `/var/lib/broker-gateway/tws-watchdog-state.json`, Logs in
`/var/log/tws-watchdog.log`.

### Installation auf cma-pi-1

```bash
cd /mnt/ssd/broker-gateway && git pull
# venv existiert bereits fuer doc-drift; sonst:
#   python3 -m venv .venv && .venv/bin/pip install httpx

# 1. ntfy-Topic festlegen + auf dem Handy in der ntfy-App abonnieren.
#    Topic mit Zufallssuffix waehlen (ntfy.sh-Topics sind oeffentlich).
sudo install -m 0640 -o cma -g cma /dev/null /etc/default/broker-gateway-watchdog
sudoedit /etc/default/broker-gateway-watchdog
#    Inhalt aus ops/systemd/tws-watchdog.env.example, Topic einsetzen.

# 2. Logfile + State-Verzeichnis.
sudo install -m 0640 -o cma -g cma /dev/null /var/log/tws-watchdog.log
sudo install -d -m 0750 -o root -g root /var/lib/broker-gateway

# 3. Units installieren + aktivieren.
sudo cp ops/systemd/tws-watchdog.service /etc/systemd/system/
sudo cp ops/systemd/tws-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tws-watchdog.timer

# 4. Verifikation: Probelauf + ntfy-Test.
systemctl list-timers --all | grep tws-watchdog
sudo systemctl start tws-watchdog.service     # einmaliger Lauf
tail -n 30 /var/log/tws-watchdog.log
#    Smoke-Test des Alarmwegs (ohne echten Ausfall):
curl -d "tws-watchdog Smoke-Test" "$BG_WATCHDOG_NTFY_URL"
```

### Tuning

| Env (in `/etc/default/broker-gateway-watchdog`) | Default | Wirkung |
|---|---|---|
| `BG_WATCHDOG_NTFY_URL` | – (Pflicht) | ntfy-Topic-URL |
| `BG_WATCHDOG_DOWN_THRESHOLD` | `2` | konsekutive 15-min-Laeufe vor Alarm |
| `BG_WATCHDOG_REALERT_HOURS` | `6` | Mindestabstand zwischen Alarmen |

## 4. Praeventiv: AutoRestartTime (taeglicher 2FA-freier Reset)

**Karte:** `387cf5c0` (AP-15) · **Bezug:** v2.10.0 (gemeinsamer Release mit
dem reaktiven Remote-Restart-Loop, ebenfalls AP-15)

Root Cause des wiederkehrenden naechtlichen Live-Ausfalls (siehe Memory
`project_live_nightly_reset_2fa`, Vorfaelle 28.06. und 30.06.2026) ist
NICHT ein Crash, sondern der taegliche IBKR-Pflicht-Logoff (~23:45 CEST),
der mangels `AutoRestartTime` in der IBC-Config eine volle
Re-Authentifizierung inklusive 2FA-Push erzwingt. Nachts bestaetigt
niemand den Push -> Timeout -> Listener bleibt tot.

### Mechanik

Das gnzsnz-Image (`ghcr.io/gnzsnz/ib-gateway:stable`) rendert die
Umgebungsvariable `AUTO_RESTART_TIME` per envsubst in
`AutoRestartTime=` in der IBC-`config.ini` (gleiche Mechanik wie
`TWOFA_DEVICE` -> `SecondFactorDevice`; verifiziert am laufenden
Live-Container, IB Gateway 10.45, `config.ini.tmpl` Sektion 4). Ist der
Wert gesetzt, fuehrt IB Gateway zur angegebenen Uhrzeit einen
Soft-Restart durch und schreibt ein **autorestart-Token**; der
folgende Re-Login nutzt dieses Token und verlangt **keine volle
Auth/kein 2FA** (Boot-Log zeigt dann `autorestart file ... found`
statt bisher `autorestart file not found: full authentication will be
required`).

`compose.yaml` setzt dafuer:

```yaml
- AUTO_RESTART_TIME=${BG_TWS_AUTO_RESTART_TIME:-}
```

Pflichtformat laut IBC-Template: `HH:MM AM/PM` mit genau einem
Leerzeichen (z. B. `06:00 AM`). Default leer = heutiges Verhalten
(keine Aenderung). Gesetzt wird der Wert **nur fuer Live** ueber
`BG_TWS_AUTO_RESTART_TIME` in `/mnt/ssd/broker-gateway/.env` — Paper
(`.env.paper`) bleibt bewusst unveraendert (Auto-Login ohne 2FA, siehe
`project_paper_login_no_2fa`), damit die gemeinsame Image-Config den
Paper-Pfad nicht anfasst. Empfohlene Uhrzeit: eine wache Tageszeit
(z. B. `06:00 AM` Europe/Vienna), damit ein evtl. doch noetiger
2FA-Push tagsueber statt nachts kommt.

Der **woechentliche** IBKR-Server-Reset (Sonntag) bleibt bewusst
2FA-pflichtig — dafuer ist `ColdRestartTime` (`TWS_COLD_RESTART`)
absichtlich NICHT gesetzt. Dieser verbleibende woechentliche Restart
wird ueber den reaktiven Remote-Trigger (AP-15, separate Karte)
bequem vom Handy ausloesbar gemacht.

### Verifikationsstatus

**Empirisch noch offen.** Die Config-Zeile allein ist kein Beleg, dass
IBCs `AutoRestartTime` den server-erzwungenen IBKR-Logoff zuverlaessig
vorwegnimmt. Verifikation erfordert einen echten Tageswechsel auf
Live:

```bash
# Am naechsten Morgen nach dem konfigurierten AUTO_RESTART_TIME pruefen:
docker logs broker-gateway-tws --since 24h | grep -iE "autorestart file|full authentication|Login has completed"
# Erwartet: "autorestart file ... found" (nicht "not found"), KEIN
# 2FA-Dialog-Log, durchgehend connected=true im gateway-tws-health.
```

Bis dahin gilt weiterhin Schritt 2 (manueller Recovery) als Fallback,
falls die Nacht trotzdem in `tws_down` endet. Rollback bei Bedarf:
`BG_TWS_AUTO_RESTART_TIME` in der Pi-`.env` leeren + `tws
force-recreate` stellt den alten Zustand wieder her.

## 5. Ursachen-Forensik (fuer die Zukunft)

Der Prozesstod vom 22.06. liess sich **nicht** rekonstruieren: der
Recovery-force-recreate hat die Container-Logs des toten Containers
verworfen. Damit eine kuenftige Analyse moeglich ist, **vor** einem
Recovery-Recreate die Logs sichern:

```bash
docker logs broker-gateway-tws > /tmp/tws-crash-$(date +%Y%m%d-%H%M).log 2>&1
```

Verwandt: [`03-deployment.md`](../03-deployment.md) (Deploy/Restart),
Memory `project_live_recovery_workflow`.
