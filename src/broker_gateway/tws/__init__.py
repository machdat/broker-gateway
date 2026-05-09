"""TWS-API-Adapter ueber ib_async (Karte 441b53db).

Pendant zu broker_gateway.cp/ - aber gegen die TWS-Socket-API statt
gegen das Client Portal Gateway. Read-Only-Pfade. Order-Routing folgt
in einer separaten Karte.
"""
from broker_gateway.tws.client import ClientIdPool, ContractNotFoundError, TWSClient

__all__ = ["ClientIdPool", "ContractNotFoundError", "TWSClient"]
