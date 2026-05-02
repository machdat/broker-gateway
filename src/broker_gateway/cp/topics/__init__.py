"""Topic-Adapter-Layer fuer den IBKR-Client-Portal-WebSocket-Stream.

Pro IBKR-Topic (smd, sor, ...) gibt es genau einen Adapter, der rohe WS-Frames
in semantisch saubere, voll-snapshot-orientierte Frames uebersetzt. Die
Adapter sind reine Python-Komponenten ohne REST-/IO-Abhaengigkeit; sie
werden vom ``WSPushSource`` (AP-11 K3) im Stream-Pfad eingehaengt.
"""
from broker_gateway.cp.topics.smd import SmdFrame, SmdTopicAdapter

__all__ = ["SmdFrame", "SmdTopicAdapter"]
