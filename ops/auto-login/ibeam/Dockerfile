# broker-gateway-paper-ibeam: Custom IBeam-Image fuer Paper-Auto-Login
# auf cma-pi-1 (ARM64). Basis ist voyz/ibeam:0.5.12. Der Patch fixt
# zwei Bugs, die einen Login auf der React-basierten IBKR-Loginpage
# verhindern (Stale-Element nach Live/Paper-Toggle, Tuple-Unpack in
# ibeam_starter.py). Diff in broker-gateway.patch.
#
# Build (auf cma-pi-1, ARM64):
#   docker build --platform linux/arm64 \
#     -t broker-gateway-paper-ibeam:1.30.0 \
#     -f ops/auto-login/ibeam/Dockerfile ops/auto-login/ibeam/
#
# Manueller Smoke (Phase-1-PoC):
#   docker run --rm --network broker-gateway-paper_default \
#     -e IBEAM_ACCOUNT=cborlm399 -e IBEAM_PASSWORD \
#     -e IBEAM_GATEWAY_BASE_URL=http://broker-gateway-paper-cpgateway:5000 \
#     -e IBEAM_USE_PAPER_ACCOUNT=true \
#     broker-gateway-paper-ibeam:1.30.0 python ibeam_starter.py -a -v

FROM voyz/ibeam:0.5.12

USER root

COPY broker-gateway.patch /tmp/broker-gateway.patch

RUN apt-get update && \
    apt-get install -y --no-install-recommends patch dos2unix && \
    rm -rf /var/lib/apt/lists/* && \
    dos2unix /srv/ibeam/src/handlers/login_handler.py /srv/ibeam/ibeam_starter.py && \
    patch -p1 -d /srv/ibeam < /tmp/broker-gateway.patch && \
    rm /tmp/broker-gateway.patch

USER basic_user
