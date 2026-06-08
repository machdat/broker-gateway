#!/usr/bin/env bash
# Notfall-Roll-Back vom TWS-Backend zurueck auf cpgateway (Karte 5).
#
# Wann benutzen:
#   - tws-Container kommt nach Live-Cutover nicht healthy hoch.
#   - 2FA-Pfad ist zu instabil (z.B. User-Handy nicht verfuegbar
#     waehrend Saturday-Reset).
#   - ib_async-Adapter zeigt unerwartete Schema-Drift gegenueber den
#     v1-API-Konsumenten.
#
# Was das Skript macht:
#   1. tws-Container stoppen.
#   2. ops/build-gateway.sh --env=$ENV --backend=cp aufrufen
#      (build + up -d gateway cpgateway via Profile cp-legacy).
#   3. Browser-Login-Hinweis ausgeben (cpgateway braucht initial
#      einen Login-Roundtrip ueber 127.0.0.1:5000 bzw. :5001).
#
# Aufruf:
#   ./ops/rollback-to-cp.sh --env=live
#   ./ops/rollback-to-cp.sh --env=paper
#
# Cutover-zurueck: ./ops/cutover-tws.sh --env=$ENV

set -euo pipefail
cd "$(dirname "$0")/.."

ENV_NAME=""
for arg in "$@"; do
    case "$arg" in
        --env=*) ENV_NAME="${arg#--env=}" ;;
        --help|-h)
            sed -n '2,24p' "$0"
            exit 0
            ;;
        *)
            echo "rollback-to-cp.sh: unbekannter Parameter '$arg'" >&2
            exit 2
            ;;
    esac
done

if [[ -z "$ENV_NAME" ]]; then
    echo "rollback-to-cp.sh: --env=live oder --env=paper Pflicht" >&2
    exit 2
fi

case "$ENV_NAME" in
    live)
        STACK_NAME="broker-gateway"
        CPGATEWAY_PORT="5000"
        ;;
    paper)
        STACK_NAME="broker-gateway-paper"
        CPGATEWAY_PORT="5001"
        ;;
    *)
        echo "rollback-to-cp.sh: --env=$ENV_NAME unbekannt (live | paper)" >&2
        exit 2
        ;;
esac

echo "=== Roll-Back $STACK_NAME -> cpgateway ==="
echo "Schritt 1: tws-Container stoppen"
docker stop "${STACK_NAME}-tws" 2>/dev/null || echo "  (tws war nicht laufend)"

echo "Schritt 2: build + up cpgateway-Stack"
SKIP_ACCEPTANCE=1 ./ops/build-gateway.sh --env="$ENV_NAME" --backend=cp

echo "Schritt 3: Status-Report"
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "${STACK_NAME}|^NAMES"
echo
echo "=== Roll-Back $STACK_NAME initial done ==="
echo
echo "WICHTIG: cpgateway braucht jetzt einen Browser-Login, sonst bleibt"
echo "die Session unauthenticated (Memory project_browser_login_not_authenticated)."
echo "  SSH-Tunnel: ssh -N -L ${CPGATEWAY_PORT}:127.0.0.1:${CPGATEWAY_PORT} cma@cma-pi-1"
echo "  Browser:    https://127.0.0.1:${CPGATEWAY_PORT}"
echo "  Anleitung:  docs/runbooks/cpgateway-login.md"
