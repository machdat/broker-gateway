# systemd-Units fuer broker-gateway

Dieses Verzeichnis enthaelt systemd-Templates fuer periodische Ops-Jobs.
Sie werden bewusst **nicht** automatisch deployed - die Installation auf
cma-pi-1 ist eine bewusste Aktion mit echten Secrets (KANPROMPT_API_KEY
bzw. ntfy-Topic), die im Repo nicht erscheinen.

## Dateien

| Datei | Zweck |
|-------|-------|
| `doc-drift.service` / `.timer` | taeglicher IBKR-OpenAPI-Doku-Drift-Check (AP-03), `scripts/check_doc_drift.py --auto-card` |
| `doc-drift.env.example` | Vorlage fuer `/etc/default/doc-drift` (Schluessel + Konfig) |
| `tws-watchdog.service` / `.timer` | tws-Stack-Watchdog alle 15 min (Karte `53c10ff4`), `scripts/tws_watchdog.py`: ntfy-Push-Alarm bei dauerhaftem `tws_down` + Paper-Auto-Recovery. Installation + Details: [`docs/runbooks/tws-recovery.md`](../../docs/runbooks/tws-recovery.md) |
| `tws-command-listener.service` | always-on ntfy-Command-Listener (Karte `a529e59a`, AP-15), `scripts/tws_command_listener.py`: ferngesteuerter Live-tws-force-recreate mit Bestätigungs-Round-Trip. Installation + Ablauf: [`docs/runbooks/tws-recovery.md`](../../docs/runbooks/tws-recovery.md) Abschnitt 4 |
| `tws-watchdog.env.example` | Vorlage fuer `/etc/default/broker-gateway-watchdog` (ntfy-Alarm-Topic + Command-Listener-`BG_CMD_*`) |

> Die Installations-Schritte unten beziehen sich auf **doc-drift**. Der
> tws-Watchdog hat einen eigenen Installations-Abschnitt im Runbook
> [`docs/runbooks/tws-recovery.md`](../../docs/runbooks/tws-recovery.md).

## Installation auf cma-pi-1

```bash
# 1. Repo aktuell halten (laeuft sowieso ueber den deploy-Pfad).
cd /mnt/ssd/broker-gateway && git pull

# 2. venv anlegen + httpx installieren (nur einmal).
python3 -m venv .venv
.venv/bin/pip install httpx

# 3. Sensitive Konfig anlegen. Schluessel kommt aus
#    ~/.claude/kanprompt-broker-gateway-api-key.txt (lokal beim User).
sudo install -m 0640 -o cma -g cma /dev/null /etc/default/doc-drift
sudoedit /etc/default/doc-drift
# Inhalt aus ops/systemd/doc-drift.env.example uebernehmen, Key einsetzen.

# 4. Logfile vorbereiten.
sudo install -m 0640 -o cma -g cma /dev/null /var/log/doc-drift.log
# Optional Logrotate-Eintrag, wenn das Logfile groesser werden soll als ein paar MB:
# /etc/logrotate.d/doc-drift mit weekly + rotate 8 + compress.

# 5. Unit + Timer installieren.
sudo cp ops/systemd/doc-drift.service /etc/systemd/system/
sudo cp ops/systemd/doc-drift.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now doc-drift.timer

# 6. Verifikation.
systemctl list-timers --all | grep doc-drift
sudo systemctl start doc-drift.service   # einmaliger Probelauf
tail -n 50 /var/log/doc-drift.log
```

## Was das Skript macht

1. Laedt die Live-IBKR-OpenAPI-Spec.
2. Vergleicht gegen die eingecheckte Baseline `docs/research/ibkr-cpapi-doc.json`.
3. Schreibt einen Bericht nach `reports/doc-drift/<heute>.md`.
4. Bei Drift (minor oder breaking) wird via KanPrompt-REST-API eine Karte
   angelegt - genau eine pro Tag pro Drift-Klasse (Spam-Schutz).

Exit-Code-Mapping zur Cron-Sichtbarkeit:

| Code | Bedeutung |
|------|-----------|
| 0 | kein Drift (oder nur value drift) - Service-Status `inactive (dead)` |
| 1 | breaking drift - `failed` Status, Karte mit blocked=true |
| 2 | minor drift - `failed` Status, Karte ohne block |
| 3 | Quell-URL nicht erreichbar - `failed` Status, keine Karte |

## Updates am Repo nach Deploy

Wenn das Skript via `git pull` aktualisiert wird, reicht ein
`systemctl restart doc-drift.service` nicht (es ist Type=oneshot). Der
naechste Timer-Trigger nutzt automatisch den neuen Code. Manuelle
Sofort-Verifikation: `sudo systemctl start doc-drift.service`.
