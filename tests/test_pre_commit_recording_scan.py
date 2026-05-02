"""Tests fuer scripts/pre_commit_recording_scan.py.

Wir importieren das Skript-Modul direkt (statt es als Subprocess zu
starten) und testen die Hauptfunktion ``main()`` - das gibt sauberen
Coverage-Pfad und schlanke Tests. Das Skript liegt nicht im
``src/``-Tree, daher wird es ueber ``importlib.util`` aus dem
absoluten Pfad geladen.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "pre_commit_recording_scan.py"


@pytest.fixture(scope="module")
def scan_module():
    spec = importlib.util.spec_from_file_location(
        "pre_commit_recording_scan", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_recording(
    tmp_path: Path,
    *,
    name: str = "iserver_auth_status__GET__noquery_01.json",
    payload: dict | None = None,
) -> Path:
    """Erzeugt eine Recording-Datei unter dem erwarteten Pfad-Schema."""
    recording_dir = tmp_path / "tests" / "fixtures" / "recorded" / "live"
    recording_dir.mkdir(parents=True, exist_ok=True)
    target = recording_dir / name
    target.write_text(
        json.dumps(payload or {}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def _clean_payload() -> dict:
    """Standard-Recording, das den Hook passieren muss."""
    return {
        "request": {
            "method": "GET",
            "url": "/iserver/auth/status",
            "query": {},
            "headers": {
                "host": "localhost:5000",
                "accept": "*/*",
                "accept-encoding": "gzip, deflate",
                "connection": "keep-alive",
                "user-agent": "python-httpx/0.28.1",
            },
            "body_json": None,
            "body_text": None,
        },
        "response": {
            "status_code": 200,
            "headers": {
                "content-type": "application/json; charset=utf-8",
                "x-content-type-options": "nosniff",
                "cache-control": "max-age=0, no-cache, no-store",
            },
            "body_json": {
                "authenticated": True,
                "MAC": "<REDACTED>",
                "hardware_info": "<REDACTED>",
            },
            "body_text": None,
        },
        "recorded_at": "2026-04-28T07:48:18.334253+00:00",
        "normalized": True,
    }


def test_clean_recording_passes(tmp_path: Path, scan_module) -> None:
    target = _make_recording(tmp_path, payload=_clean_payload())
    assert scan_module.main([str(target)]) == 0


def test_authorization_header_is_rejected(tmp_path: Path, scan_module) -> None:
    payload = _clean_payload()
    payload["request"]["headers"]["Authorization"] = "Bearer scrubbed-anyway"
    target = _make_recording(tmp_path, payload=payload)
    assert scan_module.main([str(target)]) == 1


def test_cookie_header_is_rejected(tmp_path: Path, scan_module) -> None:
    payload = _clean_payload()
    payload["request"]["headers"]["Cookie"] = "session=anything"
    target = _make_recording(tmp_path, payload=payload)
    assert scan_module.main([str(target)]) == 1


def test_urlsafe_token_in_body_is_rejected(tmp_path: Path, scan_module) -> None:
    payload = _clean_payload()
    # 40 Zeichen, URL-safe - typisch fuer ein Bearer-Token.
    payload["response"]["body_json"]["leaked_token"] = (
        "Aabcdefghijklmnopqrstuvwxyz0123456789ABCD"
    )
    target = _make_recording(tmp_path, payload=payload)
    assert scan_module.main([str(target)]) == 1


def test_short_token_is_allowed(tmp_path: Path, scan_module) -> None:
    """Strings unter 32 Zeichen sollen NICHT als Token-Verdacht gelten."""
    payload = _clean_payload()
    payload["response"]["body_json"]["short_id"] = "abc123_DEFGHIJK"  # 15 Zeichen
    target = _make_recording(tmp_path, payload=payload)
    assert scan_module.main([str(target)]) == 0


def test_sha256_in_allowlist_field_is_allowed(tmp_path: Path, scan_module) -> None:
    """SHA256-Hashes in ``MAC`` (Allowlist) duerfen den Hook nicht triggern."""
    payload = _clean_payload()
    # Echte Form: 32-stelliger Hex (typischer IBKR-MAC-Hash).
    payload["response"]["body_json"]["MAC"] = "0123456789abcdef0123456789abcdef"
    target = _make_recording(tmp_path, payload=payload)
    assert scan_module.main([str(target)]) == 0


def test_cookie_pattern_substring_is_rejected(tmp_path: Path, scan_module) -> None:
    payload = _clean_payload()
    payload["response"]["body_json"]["set_cookie_dump"] = (
        "JSESSIONID=somevalue; Path=/"
    )
    target = _make_recording(tmp_path, payload=payload)
    assert scan_module.main([str(target)]) == 1


def test_non_recording_path_is_skipped(tmp_path: Path, scan_module) -> None:
    """Ein Pfad ausserhalb tests/fixtures/recorded/ wird ignoriert."""
    other = tmp_path / "some_other.json"
    other.write_text(
        json.dumps({"Authorization": "Bearer leak-still-ok-here"}),
        encoding="utf-8",
    )
    # Datei enthaelt das verbotene Header-Wort, liegt aber nicht unter dem
    # Recording-Pfad - der Hook ist nur fuer Recordings zustaendig.
    assert scan_module.main([str(other)]) == 0


def test_no_args_returns_zero(scan_module) -> None:
    """Ohne Argumente ist der Hook ein No-Op."""
    assert scan_module.main([]) == 0


def test_jsonl_recording_ws_skips_urlsafe_check(tmp_path: Path, scan_module) -> None:
    """WS-Recordings (32-Zeichen-Session-IDs als Frame-IDs) muessen passieren."""
    ws_dir = tmp_path / "tests" / "fixtures" / "recorded" / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    target = ws_dir / "spike-test.jsonl"
    lines = [
        json.dumps(
            {
                "raw": "0123456789abcdef0123456789abcdef",
                "parsed": {"id": "0123456789abcdef0123456789abcdef"},
            }
        )
        for _ in range(3)
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert scan_module.main([str(target)]) == 0


def test_jsonl_recording_ws_still_catches_authorization_header(
    tmp_path: Path, scan_module
) -> None:
    """Header-Check bleibt auch in WS-Recordings aktiv."""
    ws_dir = tmp_path / "tests" / "fixtures" / "recorded" / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    target = ws_dir / "spike-leak.jsonl"
    target.write_text(
        json.dumps({"Authorization": "Bearer xyz", "raw": "frame"}) + "\n",
        encoding="utf-8",
    )
    assert scan_module.main([str(target)]) == 1


def test_invalid_json_returns_error_exit_code(tmp_path: Path, scan_module) -> None:
    target = _make_recording(tmp_path)
    target.write_text("{ this is not valid json", encoding="utf-8")
    # main() fängt JSONDecodeError und liefert Exit-Code 2.
    assert scan_module.main([str(target)]) == 2
