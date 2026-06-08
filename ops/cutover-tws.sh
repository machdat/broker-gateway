#!/usr/bin/env bash
# Cutover-Wrapper fuer den TWS-Backend-Wechsel (Karte 5 / v2.0.0).
#
# Was das Skript macht:
#   1. Live- (oder Paper-) cpgateway-Container stoppen, damit der
#      Single-Session-Konflikt mit dem hochfahrenden tws-Container
#      nicht zuschlaegt.
#   2. ops/build-gateway.sh --env=$ENV --backend=tws aufrufen
#      (build + up -d gateway tws).
#   3. Auf den /v1/health-Endpoint warten (max 240s = 4min, weil
#      gnzsnz/IBC + Live-2FA brauchen Zeit).
#   4. Status-Report ausgeben (Container + Health-Body).
#
# Aufruf:
#   ./ops/cutover-tws.sh --env=live    # Live-Stack (chmangold + 2FA)
#   ./ops/cutover-tws.sh --env=paper   # Paper-Stack (cborlm399, kein 2FA)
#
# Voraussetzung:
#   - BG_TWS_USERNAME und BG_TWS_PASSWORD muessen gesetzt sein
#     (Live: /etc/default/broker-gateway, Paper: /etc/default/broker-gateway-paper).
#   - Bei Live: User muss am Handy verfuegbar sein, IBC startet den
#     2FA-Push beim ersten Login-Versuch.
#
# Roll-Back: ./ops/rollback-to-cp.sh --env=$ENV

set -euo pipefail
cd "$(dirname "$0")/.."

ENV_NAME=""
for arg in "$@"; do
    case "$arg" in
        --env=*) ENV_NAME="${arg#--env=}" ;;
        --help|-h)
            sed -n '2,28p' "$0"
            exit 0
            ;;
        *)
            echo "cutover-tws.sh: unbekannter Parameter '$arg'" >&2
            exit 2
            ;;
    esac
done

if [[ -z "$ENV_NAME" ]]; then
    echo "cutover-tws.sh: --env=live oder --env=paper Pflicht" >&2
    exit 2
fi

case "$ENV_NAME" in
    live)
        STACK_NAME="broker-gateway"
        STACK_PORT="4000"
        ;;
    paper)
        STACK_NAME="broker-gateway-paper"
        STACK_PORT="4001"
        ;;
    *)
        echo "cutover-tws.sh: --env=$ENV_NAME unbekannt (live | paper)" >&2
        exit 2
        ;;
esac

echo "=== Cutover $STACK_NAME -> TWS-Backend ==="
echo "Schritt 1: cpgateway-Container stoppen (Single-Session-Konflikt vermeiden)"
docker stop "${STACK_NAME}-cpgateway" 2>/dev/null || echo "  (cpgateway war nicht laufend)"

echo "Schritt 2: build + up TWS-Stack via build-gateway.sh"
SKIP_ACCEPTANCE=1 ./ops/build-gateway.sh --env="$ENV_NAME" --backend=tws

echo "Schritt 3: warten auf /v1/health (bis zu 240s, IBC+2FA brauchen Zeit)"
for i in {1..48}; do
    if curl -sS -m 3 "http://127.0.0.1:${STACK_PORT}/v1/health" 2>/dev/null | grep -q '"status":"ok"'; then
        echo "  /v1/health antwortet 200 (Versuch $i)"
        break
    fi
    if [[ $i -eq 48 ]]; then
        echo "  /v1/health antwortet nach 240s nicht. Logs pruefen:"
        echo "    docker logs $STACK_NAME --tail 30"
        echo "    docker logs ${STACK_NAME}-tws --tail 30"
        exit 3
    fi
    sleep 5
done

echo "Schritt 4: Status-Report"
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "${STACK_NAME}|^NAMES"
echo
echo "Health-Body:"
curl -sS "http://127.0.0.1:${STACK_PORT}/v1/health"
echo

echo "=== Cutover $STACK_NAME erfolgreich ==="
echo "Roll-Back-Pfad: ./ops/rollback-to-cp.sh --env=$ENV_NAME"
