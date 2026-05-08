"""Smoke-Test fuer den broker-gateway-tws-Container (Karte 8b1781d3).

Connectet via ib_async gegen das IB Gateway 10.45.1e (gnzsnz-Image) und
liest Account-Summary fuer das Paper-Konto DUP799747.

Aufruf-Pfad (siehe ops/tws/README.md "Smoke-Test"):

    docker run --rm --network container:bg-tws-smoke-tws \\
        -v $PWD/scripts/smoke_tws.py:/smoke.py:ro \\
        python:3.13-slim bash -c "pip install -q ib_async && python /smoke.py"

Wichtig: Network-Namespace mit dem tws-Container muss geteilt sein,
weil IB Gateway derzeit nur Loopback-Sources (127.0.0.1) ohne UI-Dialog
durchlaesst (siehe ops/tws/README.md "Bekanntes Issue: Connect aus dem
Bridge-Netz"). Wer das Skript anders verkabeln will (z.B. Host-Connect
auf 127.0.0.1:4102), wird auf einen Timeout laufen.
"""

import asyncio
import os
import sys

from ib_async import IB


async def main() -> int:
    host = os.environ.get("TWS_HOST", "127.0.0.1")
    port = int(os.environ.get("TWS_PORT", "4002"))
    client_id = int(os.environ.get("TWS_CLIENT_ID", "199"))

    ib = IB()
    print(f"connecting host={host} port={port} clientId={client_id}")
    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=20)
    except TimeoutError:
        print(
            "ERROR: connect timeout. Pruefen ob der tws-Container laeuft und "
            "der Smoke-Container das Network-Namespace mit ihm teilt "
            "(--network container:bg-tws-smoke-tws).",
            file=sys.stderr,
        )
        return 2

    print(
        f"connected={ib.isConnected()}  server_version={ib.client.serverVersion()}"
    )

    print("--- Account Summary ---")
    interesting = {
        "AccountType",
        "AvailableFunds",
        "BuyingPower",
        "NetLiquidation",
        "TotalCashValue",
    }
    rows = await ib.accountSummaryAsync()
    for row in rows:
        if row.tag in interesting:
            print(f"  {row.account}  {row.tag:18s} {row.value} {row.currency}")

    ib.disconnect()
    print("disconnected.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
