"""Diff-Logik fuer Mock-Drift-Checks (AP-02 #06).

Single Source of Truth fuer den Vergleich Live-Antwort vs. Fixture.
``scripts/check_mock_drift.py`` und das ``refresh``-Subkommando in
``scripts/recording_session.py`` rufen ausschliesslich :func:`diff_recording`
auf - keine Inline-Diffs.

Klassifikation einer Drift:

- **no drift** - alle Felder identisch (oder ignoriert).
- **minor drift (additive)** - nur neue Felder hinzugekommen. Schema bleibt
  rueckwaertskompatibel; Konsumenten brechen nicht.
- **value drift** - Skalar-Wert geaendert, Schema unveraendert. Sichtbar
  fuer Konsumenten, aber kein Schema-Bruch.
- **breaking drift** - Felder entfernt, Typaenderung, oder Wert-zu-null.
  Sofortige Eskalation noetig.

Ignore-Felder kommen aus :mod:`broker_gateway.cp.normalize` plus eigenen
Listen-Eintraegen, die der Recorder schon zu Platzhaltern macht. Damit
zeigt der Diff niemals Drift bei Timestamps, Order-IDs oder Session-IDs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Felder, die der Recorder bereits zu Platzhaltern normalisiert. Drift in
# diesen Feldern ist kein Schema-Signal, sondern nur Run-Variation.
DEFAULT_IGNORE_FIELDS: frozenset[str] = frozenset({
    # Timestamps (vgl. cp/normalize.py::_TIMESTAMP_FIELDS_LOWER)
    "recorded_at", "trade_time", "ssoexpires", "_updated",
    "lastupdated", "last_updated", "expiry", "starttime", "endtime",
    # Identifier (vgl. cp/normalize.py::_ID_FIELDS_LOWER)
    "order_id", "orderid", "execution_id", "executionid",
    "exec_id", "execid", "reply_id", "replyid",
    # Session-IDs (vgl. cp/normalize.py::_SESSION_FIELDS_LOWER)
    "session", "sessionid", "session_id",
    # IBKR-spezifische Run-Variation: serverInfo enthaelt die Hardware-
    # Adresse des Backend-Servers (MAC, serverName) - aendert sich
    # zwischen Pi-Restarts ohne Schema-Bedeutung.
    "mac", "servername", "serverversion",
})


@dataclass
class FieldChange:
    """Ein einzelner geaenderter Wert/Typ an Pfad ``path``."""

    path: str
    old: Any = None
    new: Any = None
    note: str = ""


@dataclass
class DiffReport:
    """Sammelt alle Drift-Befunde und klassifiziert das Gesamtbild."""

    added_fields: list[str] = field(default_factory=list)
    removed_fields: list[str] = field(default_factory=list)
    changed_values: list[FieldChange] = field(default_factory=list)
    changed_types: list[FieldChange] = field(default_factory=list)

    def is_clean(self) -> bool:
        return not (
            self.added_fields
            or self.removed_fields
            or self.changed_values
            or self.changed_types
        )

    def is_breaking(self) -> bool:
        if self.removed_fields or self.changed_types:
            return True
        # Wert-zu-null behandeln wir wie "Feld weg" - Konsument verliert
        # den Wert und faellt evtl. auf None-Pfade, die er nicht erwartet.
        for change in self.changed_values:
            if change.new is None and change.old is not None:
                return True
        return False

    def is_minor(self) -> bool:
        # Nur additive Drift, keine Wertaenderungen.
        return bool(self.added_fields) and not (
            self.removed_fields or self.changed_values or self.changed_types
        )

    @property
    def classification(self) -> str:
        if self.is_clean():
            return "no drift"
        if self.is_breaking():
            return "breaking drift"
        if self.is_minor():
            return "minor drift (additive)"
        return "value drift"

    def render_markdown(self, *, title: str) -> str:
        lines: list[str] = [f"### {title}", "", f"**Klassifikation:** {self.classification}", ""]
        if self.is_clean():
            lines.append("- no drift")
            return "\n".join(lines) + "\n"

        if self.added_fields:
            lines.append("**Hinzugekommen (additive):**")
            lines.extend(f"- `{path}`" for path in self.added_fields)
            lines.append("")
        if self.removed_fields:
            lines.append("**Entfernt (BREAKING):**")
            lines.extend(f"- `{path}`" for path in self.removed_fields)
            lines.append("")
        if self.changed_types:
            lines.append("**Typaenderungen (BREAKING):**")
            for change in self.changed_types:
                lines.append(
                    f"- `{change.path}`: {_type_name(change.old)} -> {_type_name(change.new)}"
                )
            lines.append("")
        if self.changed_values:
            lines.append("**Wertaenderungen:**")
            for change in self.changed_values:
                note = f" ({change.note})" if change.note else ""
                lines.append(
                    f"- `{change.path}`: `{_truncate(change.old)}` -> `{_truncate(change.new)}`{note}"
                )
            lines.append("")
        return "\n".join(lines) + "\n"


def diff_recording(
    actual: Any,
    expected: Any,
    *,
    ignore_fields: set[str] | frozenset[str] | None = None,
    use_defaults: bool = True,
) -> DiffReport:
    """Vergleicht zwei JSON-aehnliche Strukturen und liefert einen DiffReport.

    Args:
        actual: Live-Antwort (frisch vom CP-Gateway).
        expected: Erwartet aus eingecheckter Fixture.
        ignore_fields: Zusaetzliche Feldnamen (lower-case, ohne Pfad), die
            der Diff ignorieren soll. Standardmaessig kombiniert mit
            :data:`DEFAULT_IGNORE_FIELDS`.
        use_defaults: Wenn False, werden NUR die uebergebenen ignore_fields
            beachtet - keine Defaults. Praktisch fuer Unit-Tests, die das
            Default-Set explizit umgehen wollen.
    """
    extra = set(ignore_fields) if ignore_fields else set()
    effective_ignore = (set(DEFAULT_IGNORE_FIELDS) | extra) if use_defaults else extra
    ignore_lower = {name.lower() for name in effective_ignore}

    report = DiffReport()
    _walk(actual, expected, path="", report=report, ignore=ignore_lower)
    return report


# ---- Internals ----


def _walk(
    actual: Any,
    expected: Any,
    *,
    path: str,
    report: DiffReport,
    ignore: set[str],
) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        _walk_dict(actual, expected, path=path, report=report, ignore=ignore)
        return

    if isinstance(expected, list) and isinstance(actual, list):
        _walk_list(actual, expected, path=path, report=report, ignore=ignore)
        return

    # null <-> Wert ist KEINE Typaenderung im API-Sinn, sondern Fuell-/
    # Leer-Schalten eines optionalen Feldes - in changed_values einsortieren,
    # damit is_breaking() ueber "value to null" entscheidet (Konsument
    # verliert den Wert). Beide None gleichzeitig ist kein Drift.
    if expected is None or actual is None:
        if expected is None and actual is None:
            return
        report.changed_values.append(
            FieldChange(
                path=path,
                old=expected,
                new=actual,
                note="filled-in" if expected is None else "value-to-null",
            )
        )
        return

    if type(expected) is not type(actual):
        # Sonderfall: int/float werden in JSON oft promoviert. bool wird
        # explizit als eigener Typ behandelt - Pydantic-Konsumenten brechen
        # an int/bool-Verwechslung.
        if not _is_compatible_numeric(expected, actual):
            report.changed_types.append(
                FieldChange(path=path, old=expected, new=actual)
            )
            return

    if expected != actual:
        report.changed_values.append(
            FieldChange(path=path, old=expected, new=actual)
        )


def _walk_dict(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    path: str,
    report: DiffReport,
    ignore: set[str],
) -> None:
    expected_keys = set(expected.keys())
    actual_keys = set(actual.keys())

    for key in sorted(expected_keys - actual_keys):
        if key.lower() in ignore:
            continue
        report.removed_fields.append(_join(path, key))

    for key in sorted(actual_keys - expected_keys):
        if key.lower() in ignore:
            continue
        report.added_fields.append(_join(path, key))

    for key in sorted(expected_keys & actual_keys):
        if key.lower() in ignore:
            continue
        _walk(actual[key], expected[key], path=_join(path, key), report=report, ignore=ignore)


def _walk_list(
    actual: list[Any],
    expected: list[Any],
    *,
    path: str,
    report: DiffReport,
    ignore: set[str],
) -> None:
    if len(actual) != len(expected):
        report.changed_values.append(
            FieldChange(
                path=path,
                old=len(expected),
                new=len(actual),
                note=f"length: {len(expected)} -> {len(actual)}",
            )
        )

    common = min(len(actual), len(expected))
    for index in range(common):
        item_path = f"{path}[{index}]"
        _walk(actual[index], expected[index], path=item_path, report=report, ignore=ignore)


def _join(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def _is_compatible_numeric(a: Any, b: Any) -> bool:
    """True wenn beide Werte numerisch und nicht bool sind.

    Pydantic akzeptiert int <-> float in den meisten Schemas. bool wird
    explizit ausgeschlossen, weil True == 1 und sonst False positives
    auftaeten.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    return isinstance(a, (int, float)) and isinstance(b, (int, float))


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    return type(value).__name__


def _truncate(value: Any, *, limit: int = 60) -> str:
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
