"""HAR-Redaktion fuer Phase 1.b der Auto-Login-Karte.

Liest eine HAR-Aufzeichnung des cpgateway-Logins und ersetzt
Credentials sowie Session-Cookies, ohne die Strukturinformationen
(Feldnamen, URLs, Status, Cookie-Namen, Header-Reihenfolge) zu
veraendern. Zweck: das Artefakt kann anschliessend ins Repo
unter docs/research/cpgateway-login-flow.har eingecheckt werden.

Aufruf::

    $env:BG_REDACT_USERNAME = "cborlm399"
    $env:BG_REDACT_PASSWORD = "<klartext>"
    python scripts/redact_har.py <input.har> <output.har> [<summary.json>]

Bewusst KEIN Klartext-Logging: das Skript druckt nur Anzahl
ersetzter Vorkommen und Strukturinfos. Das Passwort darf NIE im
Aufruf-Text stehen.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

REDACTED = "<REDACTED>"

# Cookies, deren Werte komplett geschwaerzt werden. Namen bleiben
# strukturerhalten, damit der Sidecar-Entwickler die Cookie-Mechanik
# nachvollziehen kann.
SENSITIVE_COOKIES = {
    "JSESSIONID",
    "x-sess-uuid",
    "URL_PARAM",
    "partnerID",
    "ibkr-portal-session",
    "XSRF-TOKEN",
}

# Header, deren Werte geschwaerzt werden. Namen bleiben.
SENSITIVE_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "x-csrf-token",
    "x-xsrf-token",
}


def mask_username(value: str) -> str:
    """cborlm399 -> cb***99 (gleiche Laenge nicht garantiert)."""
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def redact_text(text: str, username: str, password: str, masked_user: str) -> tuple[str, int, int]:
    """Ersetze Username + Passwort in Klartext und URL-encoded Form."""
    user_count = 0
    pwd_count = 0
    if password:
        encoded_pwd = urllib.parse.quote(password, safe="")
        if password in text:
            user_count_pwd = text.count(password)
            text = text.replace(password, REDACTED)
            pwd_count += user_count_pwd
        if encoded_pwd != password and encoded_pwd in text:
            cnt = text.count(encoded_pwd)
            text = text.replace(encoded_pwd, REDACTED)
            pwd_count += cnt
    if username:
        encoded_user = urllib.parse.quote(username, safe="")
        if username in text:
            cnt = text.count(username)
            text = text.replace(username, masked_user)
            user_count += cnt
        if encoded_user != username and encoded_user in text:
            cnt = text.count(encoded_user)
            text = text.replace(encoded_user, masked_user)
            user_count += cnt
    return text, user_count, pwd_count


def redact_value_dict_list(items: list[dict[str, Any]], sensitive_names: set[str]) -> int:
    """In QueryString/Cookies/Headers: Werte fuer sensitive Namen schwaerzen."""
    redacted = 0
    for item in items:
        name = (item.get("name") or "").lower() if "name" in item else ""
        if name in {n.lower() for n in sensitive_names}:
            if "value" in item and item["value"] != REDACTED:
                item["value"] = REDACTED
                redacted += 1
    return redacted


def redact_har(
    har: dict[str, Any], username: str, password: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Hauptfunktion: gibt redactete HAR + Strukturzusammenfassung zurueck."""
    masked_user = mask_username(username) if username else "***"
    stats = Counter()
    summary_entries: list[dict[str, Any]] = []

    for entry in har.get("log", {}).get("entries", []):
        req = entry.get("request", {})
        res = entry.get("response", {})

        stats["entries"] += 1

        # Cookies
        stats["cookies_redacted"] += redact_value_dict_list(
            req.get("cookies", []), SENSITIVE_COOKIES
        )
        stats["cookies_redacted"] += redact_value_dict_list(
            res.get("cookies", []), SENSITIVE_COOKIES
        )

        # Headers
        stats["headers_redacted"] += redact_value_dict_list(
            req.get("headers", []), SENSITIVE_HEADERS
        )
        stats["headers_redacted"] += redact_value_dict_list(
            res.get("headers", []), SENSITIVE_HEADERS
        )

        # Query-String
        for q in req.get("queryString", []):
            text = q.get("value", "")
            new, u, p = redact_text(text, username, password, masked_user)
            if new != text:
                q["value"] = new
                stats["query_user_hits"] += u
                stats["query_pwd_hits"] += p

        # Request-Body (postData)
        post = req.get("postData")
        post_field_names: list[str] = []
        if post:
            # Form-Parameter (params-Feld der HAR-Spec)
            for p in post.get("params", []):
                post_field_names.append(p.get("name", ""))
                if "value" in p:
                    new, u, pw = redact_text(p["value"], username, password, masked_user)
                    p["value"] = new
                    stats["body_user_hits"] += u
                    stats["body_pwd_hits"] += pw
            # Roh-Text
            if "text" in post:
                new, u, pw = redact_text(post["text"], username, password, masked_user)
                post["text"] = new
                stats["body_user_hits"] += u
                stats["body_pwd_hits"] += pw

        # Response-Body
        content = res.get("content", {})
        if "text" in content:
            new, u, pw = redact_text(content["text"], username, password, masked_user)
            content["text"] = new
            stats["resp_user_hits"] += u
            stats["resp_pwd_hits"] += pw

        summary_entries.append(
            {
                "method": req.get("method"),
                "url": req.get("url"),
                "status": res.get("status"),
                "request_content_type": next(
                    (h["value"] for h in req.get("headers", []) if h["name"].lower() == "content-type"),
                    None,
                ),
                "response_content_type": next(
                    (h["value"] for h in res.get("headers", []) if h["name"].lower() == "content-type"),
                    None,
                ),
                "post_field_names": post_field_names,
                "post_mime_type": (post or {}).get("mimeType"),
                "post_size": len((post or {}).get("text", "")) if post else 0,
                "response_size": len((content or {}).get("text", "")) if content else 0,
                "set_cookies": [c.get("name") for c in res.get("cookies", [])],
            }
        )

    summary = {
        "total_entries": len(summary_entries),
        "stats": dict(stats),
        "entries": summary_entries,
    }
    return har, summary


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: redact_har.py <input.har> <output.har> [<summary.json>]",
            file=sys.stderr,
        )
        return 2

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    summary_path = Path(sys.argv[3]) if len(sys.argv) >= 4 else dst.with_suffix(".summary.json")

    username = os.environ.get("BG_REDACT_USERNAME", "")
    password = os.environ.get("BG_REDACT_PASSWORD", "")

    if not username or not password:
        print(
            "error: BG_REDACT_USERNAME und BG_REDACT_PASSWORD muessen gesetzt sein",
            file=sys.stderr,
        )
        return 2

    if not src.exists():
        print(f"error: Eingabe nicht gefunden: {src}", file=sys.stderr)
        return 2

    with src.open("r", encoding="utf-8") as f:
        har = json.load(f)

    redacted, summary = redact_har(har, username, password)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8") as f:
        json.dump(redacted, f, indent=2, ensure_ascii=False)

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print("Redaktion fertig.")
    print(f"  Eingabe : {src}")
    print(f"  HAR     : {dst}")
    print(f"  Summary : {summary_path}")
    print(f"  Stats   : {dict(summary['stats'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
