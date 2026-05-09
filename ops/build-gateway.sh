#!/usr/bin/env bash
# AP-03: Build-Wrapper fuer den gateway-Container.
# AP-06 K2: --env=paper|live-Schalter (Default live).
#
# Reihenfolge:
#   1. docker compose build gateway
#   2. scripts/check_mock_drift.py --build-acceptance (mit warmer Session)
#   3. docker compose up -d gateway
#
# Bei Drift (Exit !=0 von Schritt 2) bricht der Build ab - der neue
# Container wird NICHT gestartet. So verhindern wir, dass ein veraltetes
# Mock-Snapshot in Produktion landet.
#
# VORAUSSETZUNG: Browser-Login + Reauth muessen vorab durchgelaufen sein.
# Das Skript versucht KEINEN automatischen Login - es bricht mit Exit 3
# ab und der Build schlaegt fehl. Login-Anweisung:
#     docs/runbooks/cpgateway-login.md
#
# --env-Schalter (AP-06 K2):
#   --env=live (Default)  -> Project broker-gateway, .env, Port 4000,
#                            Volume var/cpgateway/.
#   --env=paper           -> Project broker-gateway-paper, .env.paper,
#                            Port 4001, Volume var/cpgateway-paper/.
#
# --backend-Schalter (Karte 4 + 5):
#   --backend=cp (Default) -> compose.yaml only (cpgateway-Service).
#   --backend=tws          -> compose.yaml + compose.tws.yaml. Aktiviert
#                             BG_BACKEND=tws im gateway-Service und zieht
#                             den tws-Service (gnzsnz/ib-gateway:stable)
#                             hoch. Drift-Acceptance-Check wird im tws-
#                             Modus uebersprungen, weil cpgateway nicht
#                             mehr die Quelle der Wahrheit ist.
#                             BG_TWS_PORT defaultet je nach env auf
#                             4004 (paper) bzw. 4003 (live).
#
# Aufruf:
#   ./ops/build-gateway.sh                                # Live-Default (cp)
#   ./ops/build-gateway.sh --env=paper                    # Paper-Stack (cp)
#   ./ops/build-gateway.sh --env=paper --backend=tws      # Paper auf TWS
#   ./ops/build-gateway.sh --env=live  --backend=tws      # Live auf TWS (Karte 5)
#   SKIP_ACCEPTANCE=1 ./ops/build-gateway.sh              # Notfall-Bypass

set -euo pipefail

cd "$(dirname "$0")/.."

ENV_NAME="live"
BACKEND="cp"

show_help() {
    sed -n '2,42p' "$0"
}

for arg in "$@"; do
    case "$arg" in
        --env=*)
            ENV_NAME="${arg#--env=}"
            ;;
        --backend=*)
            BACKEND="${arg#--backend=}"
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            echo "build-gateway.sh: unbekannter Parameter '$arg'" >&2
            echo "  --help fuer Aufruf-Hinweise" >&2
            exit 2
            ;;
    esac
done

case "$BACKEND" in
    cp|tws) ;;
    *)
        echo "build-gateway.sh: --backend=$BACKEND unbekannt (erlaubt: cp | tws)" >&2
        exit 2
        ;;
esac

COMPOSE_FILES=(-f compose.yaml)

case "$ENV_NAME" in
    live)
        export COMPOSE_PROJECT_NAME="broker-gateway"
        export BG_ENV_FILE=".env"
        export BG_GATEWAY_HOST_PORT="${BG_GATEWAY_HOST_PORT:-4000}"
        # Pi-Desktop Quick-Login: cpgateway-Port wird auf 127.0.0.1
        # gemappt, damit der Pi-Browser den Recovery-Login direkt ueber
        # localhost machen kann (siehe
        # docs/runbooks/cpgateway-pi-desktop-login.md).
        export BG_CPGATEWAY_HOST_PORT="${BG_CPGATEWAY_HOST_PORT:-5000}"
        export BG_CPGATEWAY_VOLUME="${BG_CPGATEWAY_VOLUME:-./var/cpgateway/logs}"
        # Defensive: setzt BG_STACK_KIND, falls die .env-Datei den
        # Wert nicht enthaelt. ENV-Wert in der env_file hat Vorrang im
        # Container, dieser Export deckt den Fall ab, in dem die
        # env_file-Variable noch fehlt.
        export BG_STACK_KIND="live"
        # Hard-Guard 3: Live-Stack bekommt KEINEN Auto-Login-Override
        # und damit keinen docker.sock-Mount. Wer hier compose.paper.
        # auto-login.yaml einhaengen wuerde, bekaeme ohnehin beim
        # Service-Startup einen ConfigError (Hard-Guard 1).
        ;;
    paper)
        export COMPOSE_PROJECT_NAME="broker-gateway-paper"
        export BG_ENV_FILE=".env.paper"
        export BG_GATEWAY_HOST_PORT="${BG_GATEWAY_HOST_PORT:-4001}"
        # Pi-Desktop Quick-Login: Paper-cpgateway nutzt 127.0.0.1:5001,
        # damit Live (5000) und Paper (5001) parallel ohne Port-Konflikt
        # auf dem Pi-Host erreichbar sind.
        export BG_CPGATEWAY_HOST_PORT="${BG_CPGATEWAY_HOST_PORT:-5001}"
        export BG_CPGATEWAY_VOLUME="${BG_CPGATEWAY_VOLUME:-/mnt/ssd/broker-gateway-paper/var/cpgateway-paper/logs}"
        export BG_STACK_KIND="paper"
        # Auto-Login-Override haengt den docker.sock-Mount und die
        # BG_PAPER_*/BG_AUTO_LOGIN_*-Vars an den gateway-Service.
        # Bewusst nur fuer Paper.
        COMPOSE_FILES+=(-f compose.paper.auto-login.yaml)
        # Optional: /etc/default/broker-gateway-paper auf dem Pi
        # haelt BG_PAPER_USERNAME/_PASSWORD/_AUTO_LOGIN. Wenn die
        # Datei existiert, in das aktuelle Env laden (set -a / source).
        if [[ -f /etc/default/broker-gateway-paper ]]; then
            set -a
            # shellcheck disable=SC1091
            . /etc/default/broker-gateway-paper
            set +a
        fi
        ;;
    *)
        echo "build-gateway.sh: --env=$ENV_NAME unbekannt (erlaubt: live | paper)" >&2
        exit 2
        ;;
esac

if [[ "$BACKEND" == "tws" ]]; then
    # Override-File ans Ende anhaengen, damit es die compose.yaml-Werte
    # ueberschreibt (BG_BACKEND, depends_on auf tws statt cpgateway).
    COMPOSE_FILES+=(-f compose.tws.yaml)
    # Default-Port je nach env: gnzsnz forwardet via socat 4001->4003
    # (live) bzw. 4002->4004 (paper). Wer beide Ports braucht, kann
    # BG_TWS_PORT explizit ueberschreiben.
    if [[ "$ENV_NAME" == "paper" ]]; then
        export BG_TWS_PORT="${BG_TWS_PORT:-4004}"
        export BG_TWS_TRADING_MODE="${BG_TWS_TRADING_MODE:-paper}"
        export BG_TWS_VNC_HOST_PORT="${BG_TWS_VNC_HOST_PORT:-5905}"
        export BG_TWS_VOLUME="${BG_TWS_VOLUME:-/mnt/ssd/broker-gateway-paper/var/tws/jts}"
    else
        export BG_TWS_PORT="${BG_TWS_PORT:-4003}"
        export BG_TWS_TRADING_MODE="${BG_TWS_TRADING_MODE:-live}"
        export BG_TWS_VNC_HOST_PORT="${BG_TWS_VNC_HOST_PORT:-5906}"
        export BG_TWS_VOLUME="${BG_TWS_VOLUME:-./var/tws/jts}"
    fi
fi

if [[ ! -f "$BG_ENV_FILE" ]]; then
    echo "build-gateway.sh: env-Datei '$BG_ENV_FILE' nicht gefunden." >&2
    if [[ "$ENV_NAME" == "paper" ]]; then
        echo "Vorlage anlegen via: cp .env.example .env.paper" >&2
        echo "(siehe docs/runbooks/paper-account-setup.md)" >&2
    fi
    exit 2
fi

GIT_COMMIT="${GIT_COMMIT:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
SKIP_ACCEPTANCE="${SKIP_ACCEPTANCE:-0}"
BASE_URL="${CPGATEWAY_BASE_URL:-http://localhost:5000/v1/api}"
PYTHON_BIN="${PYTHON_BIN:-python}"

echo "[env] $ENV_NAME (project=$COMPOSE_PROJECT_NAME, env_file=$BG_ENV_FILE, port=$BG_GATEWAY_HOST_PORT, backend=$BACKEND)"
echo "[1/4] docker compose build gateway"
docker compose --env-file "$BG_ENV_FILE" "${COMPOSE_FILES[@]}" build gateway

if [[ "$ENV_NAME" == "paper" && "$BACKEND" == "cp" ]]; then
    echo "[2/4] docker build broker-gateway-paper-auto-login (linux/arm64)"
    docker build \
        --platform linux/arm64 \
        -t "${BG_AUTO_LOGIN_IMAGE:-broker-gateway-paper-auto-login:latest}" \
        -f ops/auto-login/Dockerfile \
        ops/auto-login/
else
    echo "[2/4] SKIP: --env=$ENV_NAME --backend=$BACKEND, Auto-Login-Sidecar wird nicht gebaut."
fi

if [[ "${SKIP_ACCEPTANCE}" == "1" ]]; then
    echo "[3/4] SKIP: SKIP_ACCEPTANCE=1 gesetzt - Drift-Check uebersprungen."
elif [[ "$BACKEND" == "tws" ]]; then
    echo "[3/4] SKIP: --backend=tws - cpgateway ist nicht mehr Quelle der Wahrheit, Drift-Check entfaellt."
else
    echo "[3/4] check_mock_drift --build-acceptance (commit ${GIT_COMMIT})"
    GIT_COMMIT="${GIT_COMMIT}" "${PYTHON_BIN}" scripts/check_mock_drift.py \
        --base-url "${BASE_URL}" \
        --build-acceptance
fi

echo "[4/4] docker compose up -d gateway"
if [[ "$BACKEND" == "tws" ]]; then
    docker compose --env-file "$BG_ENV_FILE" "${COMPOSE_FILES[@]}" up -d gateway tws
else
    docker compose --env-file "$BG_ENV_FILE" "${COMPOSE_FILES[@]}" up -d gateway
fi

echo "[done] gateway-Container ist live ($ENV_NAME, backend=$BACKEND)."
