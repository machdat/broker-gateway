#!/usr/bin/env bash
# AP-03: Build-Wrapper fuer den gateway-Container.
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
# Aufruf:
#   ./ops/build-gateway.sh                   # Default: skip-acceptance=0
#   SKIP_ACCEPTANCE=1 ./ops/build-gateway.sh # Notfall-Bypass (begruendet
#                                              z.B. fuer Doku-only Aenderungen)

set -euo pipefail

cd "$(dirname "$0")/.."

GIT_COMMIT="${GIT_COMMIT:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
SKIP_ACCEPTANCE="${SKIP_ACCEPTANCE:-0}"
BASE_URL="${CPGATEWAY_BASE_URL:-http://localhost:5000/v1/api}"
PYTHON_BIN="${PYTHON_BIN:-python}"

echo "[1/3] docker compose build gateway"
docker compose build gateway

if [[ "${SKIP_ACCEPTANCE}" == "1" ]]; then
    echo "[2/3] SKIP: SKIP_ACCEPTANCE=1 gesetzt - Drift-Check uebersprungen."
else
    echo "[2/3] check_mock_drift --build-acceptance (commit ${GIT_COMMIT})"
    GIT_COMMIT="${GIT_COMMIT}" "${PYTHON_BIN}" scripts/check_mock_drift.py \
        --base-url "${BASE_URL}" \
        --build-acceptance
fi

echo "[3/3] docker compose up -d gateway"
docker compose up -d gateway

echo "[done] gateway-Container ist live."
