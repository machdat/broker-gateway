"""CP-Gateway-Replay-Infrastruktur fuer pytest.

Single Source of Truth fuer Mock-Antworten ist der Inhalt von
``tests/fixtures/recorded/`` - entweder aus echten Live-Recordings
(``live/``) oder aus den seed-Bodies (``seed/``), die das alte
hartcodierte Mock-Verhalten 1:1 reproduzieren. ``ReplayCPGatewayMock``
ersetzt die frueher in ``tests/conftest.py`` gepflegte
``MockCPGateway``-Klasse, ohne deren API zu brechen.
"""
from tests.cp_mock.loader import (
    DEFAULT_FIXTURES_DIR,
    RecordingNotFoundError,
    load_recording,
)
from tests.cp_mock.replay import ReplayCPGatewayMock

__all__ = [
    "DEFAULT_FIXTURES_DIR",
    "RecordingNotFoundError",
    "ReplayCPGatewayMock",
    "load_recording",
]
