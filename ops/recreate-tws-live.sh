#!/usr/bin/env bash
# Abgesicherter One-Shot Live-tws-force-recreate (Karte c3839836, AP-15).
#
# BEWUSST GETRENNT von recreate-tws.sh: dort bleibt die sichere Paper-Default
# (jeder Nicht-Paper-Aufruf -> Exit 2) unangetastet. Ein Live-force-recreate
# loest einen Mobile-2FA-Push aus, den nur der Operator am Handy bestaetigen
# kann - deshalb wird dieses Skript NUR hinter einem expliziten Opt-in scharf.
#
# Der ntfy-Command-Listener (Folge-Karte) setzt BG_ALLOW_LIVE_RECREATE=yes und
# ruft dieses Skript nach dem Bestaetigungs-Round-Trip auf. Von der Shell aus
# ist ein versehentlicher Live-Recreate ohne dieses Env nicht moeglich.
#
# Ablauf:
#   1. Hard-Guard BG_ALLOW_LIVE_RECREATE=yes (sonst Exit 2, keine docker-Aktion)
#   2. Pre-Recreate-Forensik: Container-Logs des sterbenden tws sichern
#      (best effort, Runbook tws-recovery.md Abschnitt 4)
#   3. GENAU EIN force-recreate des tws-Containers - kein Healthcheck-Warten,
#      kein Retry (der Listener pollt separat; konkurrierende 2FA-Pushes
#      vermeiden, Runbook-Disziplin "genau EIN force-recreate")
#
# Aufruf (nur durch den Listener-Dienst gedacht):
#   BG_ALLOW_LIVE_RECREATE=yes ./ops/recreate-tws-live.sh

set -euo pipefail

# 1. Hard-Guard: ohne explizites Opt-in passiert nichts (keine docker-Aktion).
if [[ "${BG_ALLOW_LIVE_RECREATE:-}" != "yes" ]]; then
    echo "recreate-tws-live.sh: Opt-in fehlt - setze BG_ALLOW_LIVE_RECREATE=yes, um den Live-tws-force-recreate scharf zu schalten (Live braucht Mobile-2FA, siehe docs/runbooks/tws-recovery.md)." >&2
    exit 2
fi

# 2. Live-Repo-Kontext. Der Live-Stack wird aus /mnt/ssd/broker-gateway mit
#    seiner .env und COMPOSE_PROJECT_NAME=broker-gateway verwaltet.
LIVE_REPO_DIR="${BG_LIVE_REPO_DIR:-/mnt/ssd/broker-gateway}"
cd "$LIVE_REPO_DIR" 2>/dev/null || {
    echo "recreate-tws-live.sh: Live-Repo '$LIVE_REPO_DIR' nicht gefunden (BG_LIVE_REPO_DIR setzen?)." >&2
    exit 2
}

export COMPOSE_PROJECT_NAME="broker-gateway"
BG_ENV_FILE="${BG_ENV_FILE:-.env}"

if [[ ! -f "$BG_ENV_FILE" ]]; then
    echo "recreate-tws-live.sh: env-Datei '$BG_ENV_FILE' im Live-Repo '$LIVE_REPO_DIR' nicht gefunden." >&2
    exit 2
fi

# 3. Pre-Recreate-Forensik (best effort). Der bisherige Recovery-Recreate hat
#    die Logs des Vorfalls verworfen - hier werden sie VOR dem Recreate
#    gesichert. Ein Fehlschlag darf den Recreate NICHT blockieren (|| true).
CRASH_LOG_DIR="${BG_CRASH_LOG_DIR:-/tmp}"
mkdir -p "$CRASH_LOG_DIR" 2>/dev/null || true
CRASH_LOG="${CRASH_LOG_DIR}/tws-crash-$(date +%Y%m%d-%H%M%S).log"
echo "[recreate-live] Sichere tws-Container-Logs vor dem Recreate -> $CRASH_LOG"
docker logs "${COMPOSE_PROJECT_NAME}-tws" > "$CRASH_LOG" 2>&1 || true

# 4. GENAU EIN force-recreate. Kein Loop, kein Healthcheck-Warten - der
#    Exit-Code des compose-Calls wird durchgereicht (set -e).
echo "[recreate-live] Live-tws force-recreate (kein Build)..."
docker compose --env-file "$BG_ENV_FILE" -f compose.yaml up -d --force-recreate tws

echo "[done] Live-tws recreatet - IB Key am Handy bestaetigen, dann Healthcheck-Hochlauf abwarten (docker inspect Health)."
