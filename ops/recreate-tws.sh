#!/usr/bin/env bash
# Schlanker tws-Force-Recreate OHNE Build - fuer die Watchdog-Auto-Recovery
# (Karte 53c10ff4). Kapselt die Paper-Compose-Env-Logik aus build-gateway.sh,
# startet aber nur den tws-Container neu (kein gateway-Build, keine
# Acceptance-Pruefung) - so heilt sich ein toter Paper-tws-Listener selbst.
#
# BEWUSST NUR PAPER: Das Live-Konto nutzt Mobile-2FA, ein force-recreate
# loest dort einen 2FA-Push aus, den nur der Operator am Handy bestaetigen
# kann. Ein Live-Aufruf wird daher mit Exit 2 abgelehnt
# (siehe docs/runbooks/tws-recovery.md).
#
# Der Live-force-recreate liegt bewusst im getrennten Skript
# recreate-tws-live.sh hinter dem Opt-in BG_ALLOW_LIVE_RECREATE=yes (Karte
# c3839836, AP-15) - so bleibt die Paper-Safety-Default hier unangetastet.
#
# Aufruf:
#   ./ops/recreate-tws.sh paper

set -euo pipefail

ENV_NAME="${1:-}"

if [[ "$ENV_NAME" != "paper" ]]; then
    echo "recreate-tws.sh: nur 'paper' unterstuetzt (Live braucht 2FA -> manueller Recovery, siehe docs/runbooks/tws-recovery.md)." >&2
    exit 2
fi

# Der Paper-Stack wird aus seinem EIGENEN Repo-Clone verwaltet (eigene
# .env.paper + var/-Volumes + COMPOSE_PROJECT_NAME). Der Watchdog laeuft
# aus dem Live-Repo (/mnt/ssd/broker-gateway), daher MUSS hier explizit
# ins Paper-Repo gewechselt werden - NICHT ins Skript-eigene Verzeichnis,
# dort fehlt .env.paper.
PAPER_REPO_DIR="${BG_PAPER_REPO_DIR:-/mnt/ssd/broker-gateway-paper}"
cd "$PAPER_REPO_DIR" 2>/dev/null || {
    echo "recreate-tws.sh: Paper-Repo '$PAPER_REPO_DIR' nicht gefunden (BG_PAPER_REPO_DIR setzen?)." >&2
    exit 2
}

export COMPOSE_PROJECT_NAME="broker-gateway-paper"
export BG_ENV_FILE=".env.paper"
export BG_GATEWAY_HOST_PORT="${BG_GATEWAY_HOST_PORT:-4001}"
export BG_CPGATEWAY_HOST_PORT="${BG_CPGATEWAY_HOST_PORT:-5001}"
export BG_CPGATEWAY_VOLUME="${BG_CPGATEWAY_VOLUME:-/mnt/ssd/broker-gateway-paper/var/cpgateway-paper/logs}"
export BG_STACK_KIND="paper"
export BG_TWS_PORT="${BG_TWS_PORT:-4004}"
export BG_TWS_TRADING_MODE="${BG_TWS_TRADING_MODE:-paper}"
export BG_TWS_VNC_HOST_PORT="${BG_TWS_VNC_HOST_PORT:-5905}"

# Paper-Auto-Login-Vars (BG_PAPER_USERNAME/_PASSWORD/_AUTO_LOGIN) liegen
# in /etc/default/broker-gateway-paper - laden, damit der recreatete
# Container sich ohne Operator wieder einloggt (cborlm399 hat kein 2FA).
if [[ -f /etc/default/broker-gateway-paper ]]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/default/broker-gateway-paper
    set +a
fi

if [[ ! -f "$BG_ENV_FILE" ]]; then
    echo "recreate-tws.sh: env-Datei '$BG_ENV_FILE' nicht gefunden." >&2
    exit 2
fi

echo "[recreate-tws] Paper-tws force-recreate (kein Build)..."
docker compose --env-file "$BG_ENV_FILE" \
    -f compose.yaml -f compose.paper.auto-login.yaml \
    up -d --force-recreate tws

echo "[done] Paper-tws recreatet - Healthcheck-Hochlauf abwarten (docker inspect Health)."
