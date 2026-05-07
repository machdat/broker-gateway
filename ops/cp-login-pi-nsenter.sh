#!/usr/bin/env bash
# Karte 739777a9 Phase 2.1d: Login-Browser im Service-Network-Namespace.
#
# Startet Pi-Desktop-Chromium als User cma im TCP-Namespace eines
# broker-gateway-Containers (live oder paper). Damit hat der Browser
# beim Login-Submit die gleiche Source-IP wie der Service-Container —
# cpgateway bindet die Session an genau diese IP, der Service kann sie
# danach nutzen.
#
# Hintergrund: cpgateway erzwingt eine IP-basierte Session-Whitelist
# (siehe Karte 739777a9, docs/02-architecture.md). Browser auf dem
# Pi-Host und Service-Container haben verschiedene Bridge-IPs ->
# cpgateway behandelt sie als getrennte Sessions. nsenter --net teilt
# den TCP-Stack des Service-Containers, ohne X11/Wayland zu wechseln —
# der Browser bleibt im wayvnc-Pi-Desktop-Display sichtbar.
#
# Voraussetzungen (Pi cma-pi-1):
#   - User cma mit sudo NOPASSWD
#   - Pi-Desktop wayvnc-Session aktiv
#   - chromium installiert (/usr/bin/chromium)
#   - broker-gateway[-paper]-Container laeuft
#
# Aufruf vom Pi-Host (oder via SSH):
#   ./ops/cp-login-pi-nsenter.sh paper
#   ./ops/cp-login-pi-nsenter.sh live
#
# Workflow:
#   1. Aufruf -> Browser oeffnet sich auf wayvnc-Display
#   2. wayvnc-Client am Laptop oeffnen, Paper-Toggle anklicken,
#      Login eingeben, Enter
#   3. "Client login succeeds"-Page sehen
#   4. Service-Lifecycle sollte innerhalb weniger Sekunden auf
#      auth_status=ok wechseln (siehe internal/health-Snapshot).

set -euo pipefail

ENV_NAME="${1:-paper}"

case "$ENV_NAME" in
    paper)
        SVC_NAME="broker-gateway-paper"
        CP_NAME="broker-gateway-paper-cpgateway"
        ;;
    live)
        SVC_NAME="broker-gateway"
        CP_NAME="broker-gateway-cpgateway"
        ;;
    *)
        echo "Unbekanntes env: $ENV_NAME (erlaubt: paper | live)" >&2
        exit 2
        ;;
esac

if ! sudo -n true 2>/dev/null; then
    echo "sudo passwordless ist nicht aktiv. nsenter braucht sudo." >&2
    exit 1
fi

if ! command -v nsenter >/dev/null 2>&1; then
    echo "nsenter nicht installiert (apt-get install util-linux)." >&2
    exit 1
fi

if ! command -v chromium >/dev/null 2>&1; then
    echo "chromium nicht installiert (apt-get install chromium)." >&2
    exit 1
fi

SVC_PID="$(docker inspect -f '{{.State.Pid}}' "$SVC_NAME" 2>/dev/null || true)"
if [[ -z "$SVC_PID" || "$SVC_PID" == "0" ]]; then
    echo "Service-Container '$SVC_NAME' nicht gefunden / nicht aktiv." >&2
    exit 1
fi

# DNS-Quirk: nsenter wechselt nur den Network-Namespace, nicht den
# Mount-Namespace. Pi-Host-Chromium liest weiterhin Pi-Host's
# /etc/resolv.conf, dessen DNS-Server (z.B. 127.0.0.53) im Container-
# netns nicht erreichbar sind. Konsequenz: Compose-Service-Namen wie
# `broker-gateway-paper-cpgateway` werden ERR_NAME_NOT_RESOLVED. Wir
# loesen die cpgateway-IP einmalig auf und steckenden URL hart kodieren.
CP_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CP_NAME" 2>/dev/null || true)"
if [[ -z "$CP_IP" ]]; then
    echo "cpgateway-Container '$CP_NAME' hat keine IP." >&2
    exit 1
fi

LOGIN_URL="http://${CP_IP}:5000/sso/Login?forwardTo=22&RL=1&ip2loc=on"
USER_DATA_DIR="/tmp/cp-login-${ENV_NAME}"
LOG_FILE="/tmp/cp-login-${ENV_NAME}-chromium.log"

# Eventueller alter Browser-Prozess fuer dieses env raus, sonst
# kollidiert der user-data-dir-Lock.
for p in $(pgrep -f "chromium.*${USER_DATA_DIR}" 2>/dev/null || true); do
    kill "$p" 2>/dev/null || true
done
sleep 1

echo "Service-Container: ${SVC_NAME} (PID ${SVC_PID})"
echo "Login-URL:         ${LOGIN_URL}"
echo "Browser-Log:       ${LOG_FILE}"
echo

# WAYLAND_DISPLAY + XDG_RUNTIME_DIR + DBUS_SESSION_BUS_ADDRESS sind
# Pflicht — Wayland-Stack braucht alle drei, sonst startet Chromium
# nicht (DBus-Connect-Errors blockieren Init unter sway/wayfire).
# --unsafely-treat-insecure-origin-as-secure: cpgateway sendet
# Set-Cookie mit Secure-Flag (es ist eigentlich fuer HTTPS gebaut).
# Chromium verwirft Secure-Cookies ueber HTTP — ausser bei Loopback-
# Origins (127.0.0.1) oder wenn der Origin als trusted markiert ist.
# Container-Bridge-IPs (172.x.x.x) sind kein Loopback, also brauchen
# wir den Flag, sonst landet kein Cookie im Browser-Profil und der
# Login ist effektiv ohne Folgewirkung.
# --disable-web-security + --disable-features=BlockInsecure...:
# cpgateway-Login-Page macht beim Render einen JavaScript-
# Connectivity-Check via XHR auf https://www.interactivebrokers.com/
# en/includes/general/gdpr-am.php (Cookie-Consent + Reachability).
# Origin ist http://172.23.0.2:5000 -> Cross-Origin -> CORS-preflight
# scheitert (kein Access-Control-Allow-Origin auf interactivebrokers.com
# fuer http-Origins). Ohne den Disable-Flag rendert die Login-Form als
# "Network connectivity error: Unable to reach server" und der Login
# bleibt blockiert. HAR-Befund 2026-05-07 (Karte 739777a9 Phase 2).
# Sicherheitlich vertretbar fuer den Pi-Login-Container: das Profil ist
# isoliert, die einzige besuchte URL ist cpgateway selbst, --user-data-
# dir trennt den State von einem normalen Browser-Profil.
SECURE_ORIGIN="http://${CP_IP}:5000"

nohup \
    sudo -n nsenter --net="/proc/${SVC_PID}/ns/net" \
        sudo -u cma \
            env \
                XDG_RUNTIME_DIR=/run/user/1000 \
                WAYLAND_DISPLAY=wayland-0 \
                DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
            chromium \
                --ozone-platform=wayland \
                --new-window \
                --user-data-dir="${USER_DATA_DIR}" \
                --unsafely-treat-insecure-origin-as-secure="${SECURE_ORIGIN}" \
                --disable-web-security \
                --disable-features=BlockInsecurePrivateNetworkRequests \
                "${LOGIN_URL}" \
    >"${LOG_FILE}" 2>&1 </dev/null &

disown
sleep 4

if pgrep -f "chromium.*${USER_DATA_DIR}" >/dev/null 2>&1; then
    echo "Chromium gestartet — Browser-Tab auf dem wayvnc-Display oeffnen."
    echo "Sichtbar via wayvnc/VNC auf cma-pi-1:5900 (Pi-Desktop)."
else
    echo "WARN: Chromium-Prozess nicht gefunden. Log:"
    tail -10 "${LOG_FILE}" >&2
    exit 1
fi
