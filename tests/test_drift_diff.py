"""Unit-Tests fuer ``tests/cp_mock/diff.py`` - Drift-Klassifikation.

Pflicht laut AP-02 #06: jeder Klassifikationsfall einzeln getestet.
"""
from __future__ import annotations

import pytest

from tests.cp_mock.diff import (
    DEFAULT_IGNORE_FIELDS,
    DiffReport,
    diff_recording,
)


# ---- Klassifikationsfaelle ----


def test_no_drift_when_payloads_identical() -> None:
    expected = {"a": 1, "b": "x", "c": [1, 2, 3]}
    actual = {"a": 1, "b": "x", "c": [1, 2, 3]}

    report = diff_recording(actual, expected)

    assert report.is_clean()
    assert not report.is_breaking()
    assert not report.is_minor()
    assert report.classification == "no drift"


def test_added_field_is_minor_drift() -> None:
    expected = {"a": 1}
    actual = {"a": 1, "b": "neu"}

    report = diff_recording(actual, expected)

    assert report.added_fields == ["b"]
    assert report.removed_fields == []
    assert report.changed_values == []
    assert report.changed_types == []
    assert report.is_minor()
    assert not report.is_breaking()
    assert report.classification == "minor drift (additive)"


def test_removed_field_is_breaking_drift() -> None:
    expected = {"a": 1, "b": "weg"}
    actual = {"a": 1}

    report = diff_recording(actual, expected)

    assert report.removed_fields == ["b"]
    assert report.is_breaking()
    assert report.classification == "breaking drift"


def test_type_change_is_breaking_drift() -> None:
    expected = {"qty": 10}
    actual = {"qty": "10"}

    report = diff_recording(actual, expected)

    assert any(c.path == "qty" for c in report.changed_types)
    assert report.is_breaking()


def test_value_change_in_deterministic_field_is_value_drift_not_breaking() -> None:
    expected = {"server": "alpha"}
    actual = {"server": "beta"}

    report = diff_recording(actual, expected)

    assert any(c.path == "server" for c in report.changed_values)
    assert report.added_fields == []
    assert report.removed_fields == []
    assert report.changed_types == []
    assert not report.is_breaking()
    assert not report.is_clean()
    assert report.classification == "value drift"


def test_value_change_in_ignored_field_is_clean() -> None:
    expected = {"order_id": "ORDER_ID_001", "status": "OPEN"}
    actual = {"order_id": "ORDER_ID_999", "status": "OPEN"}

    report = diff_recording(actual, expected)

    assert report.is_clean()


def test_default_ignore_fields_covers_normalize_placeholders() -> None:
    assert "order_id" in DEFAULT_IGNORE_FIELDS
    assert "execution_id" in DEFAULT_IGNORE_FIELDS
    assert "session_id" in DEFAULT_IGNORE_FIELDS
    assert "recorded_at" in DEFAULT_IGNORE_FIELDS
    assert "expiry" in DEFAULT_IGNORE_FIELDS


# ---- Verschachtelte Strukturen ----


def test_added_field_nested_uses_dot_path() -> None:
    expected = {"acct": {"id": "U1"}}
    actual = {"acct": {"id": "U1", "currency": "USD"}}

    report = diff_recording(actual, expected)

    assert report.added_fields == ["acct.currency"]
    assert report.is_minor()


def test_removed_field_in_list_element_marked_breaking() -> None:
    expected = {"items": [{"a": 1, "b": 2}]}
    actual = {"items": [{"a": 1}]}

    report = diff_recording(actual, expected)

    assert "items[0].b" in report.removed_fields
    assert report.is_breaking()


def test_list_length_change_is_value_change() -> None:
    expected = {"items": [1, 2, 3]}
    actual = {"items": [1, 2]}

    report = diff_recording(actual, expected)

    assert any(c.path == "items" and "length" in c.note for c in report.changed_values)
    # Listen-Laenge ist VALUE drift, nicht breaking - hat Anwender-Wirkung,
    # aber kein Schema-Bruch.
    assert not report.is_breaking()


def test_dict_to_list_is_breaking_type_change() -> None:
    expected = {"data": {"k": "v"}}
    actual = {"data": ["v"]}

    report = diff_recording(actual, expected)

    assert any(c.path == "data" for c in report.changed_types)
    assert report.is_breaking()


def test_null_to_value_treated_as_added() -> None:
    expected = {"field": None}
    actual = {"field": "now-set"}

    report = diff_recording(actual, expected)

    # Optional-Feld bekommt Inhalt - additive Drift, weil Schema-vertraeglich.
    assert any(c.path == "field" for c in report.changed_values)
    assert not report.is_breaking()


def test_value_to_null_treated_as_removed_breaking() -> None:
    expected = {"field": "vorhanden"}
    actual = {"field": None}

    report = diff_recording(actual, expected)

    assert any(c.path == "field" for c in report.changed_values)
    # Wert verschwindet - breaking, weil Konsumenten den Wert evtl. erwarten.
    assert report.is_breaking()


def test_null_on_both_sides_is_clean() -> None:
    # Beide Seiten None ist kein Drift - sonst produziert jedes optionale,
    # immer-leere Feld bei jedem Live-Lauf eine "filled-in"-Wertaenderung
    # und der Bericht versinkt im Lauerm.
    expected = {"field": None}
    actual = {"field": None}

    report = diff_recording(actual, expected)

    assert report.is_clean()
    assert report.classification == "no drift"


# ---- ignore_fields-Override ----


def test_custom_ignore_fields_take_precedence() -> None:
    expected = {"my_id": "x", "stable": "a"}
    actual = {"my_id": "y", "stable": "a"}

    report = diff_recording(actual, expected, ignore_fields={"my_id"})

    assert report.is_clean()


def test_custom_ignore_fields_replace_defaults_only_when_explicit() -> None:
    # Wenn ignore_fields uebergeben wird, gilt es ZUSAETZLICH zu den
    # Defaults. Wer das nicht will, uebergibt einen leeren Set explizit.
    expected = {"order_id": "A", "custom": "1"}
    actual = {"order_id": "B", "custom": "2"}

    report = diff_recording(actual, expected, ignore_fields={"custom"})

    # order_id (default) UND custom (extra) ignoriert -> clean.
    assert report.is_clean()


def test_only_explicit_empty_set_disables_defaults() -> None:
    expected = {"order_id": "A"}
    actual = {"order_id": "B"}

    report = diff_recording(actual, expected, ignore_fields=set(), use_defaults=False)

    assert any(c.path == "order_id" for c in report.changed_values)


# ---- Skalar-Toplevel-Edgecase ----


def test_scalar_payloads_compared_directly() -> None:
    report_same = diff_recording(42, 42)
    assert report_same.is_clean()

    report_diff = diff_recording(43, 42)
    assert any(c.path == "" for c in report_diff.changed_values)


def test_list_payloads_top_level_supported() -> None:
    expected = [{"a": 1}, {"a": 2}]
    actual = [{"a": 1}, {"a": 2, "b": 3}]

    report = diff_recording(actual, expected)

    assert report.added_fields == ["[1].b"]
    assert report.is_minor()


# ---- Markdown-Rendering ----


def test_diff_report_renders_markdown_section() -> None:
    expected = {"a": 1}
    actual = {"a": 1, "b": "neu"}

    report = diff_recording(actual, expected)
    md = report.render_markdown(title="GET /foo")

    assert "GET /foo" in md
    assert "minor drift" in md
    assert "b" in md


def test_clean_report_renders_no_drift_marker() -> None:
    report = diff_recording({"a": 1}, {"a": 1})
    md = report.render_markdown(title="GET /bar")

    assert "no drift" in md


# ---- Boundary: gemischte Drift-Klassen ----


def test_mixed_added_and_removed_is_breaking() -> None:
    expected = {"a": 1, "b": 2}
    actual = {"a": 1, "c": 3}

    report = diff_recording(actual, expected)

    assert "c" in report.added_fields
    assert "b" in report.removed_fields
    assert report.is_breaking()


@pytest.mark.parametrize(
    "expected,actual,expected_class",
    [
        ({"a": 1}, {"a": 1}, "no drift"),
        ({"a": 1}, {"a": 1, "b": 2}, "minor drift (additive)"),
        ({"a": 1, "b": 2}, {"a": 1}, "breaking drift"),
        ({"a": 1}, {"a": "1"}, "breaking drift"),
        ({"server": "alpha"}, {"server": "beta"}, "value drift"),
    ],
)
def test_classification_string_matches_drift_kind(
    expected: dict, actual: dict, expected_class: str
) -> None:
    report = diff_recording(actual, expected)
    assert report.classification == expected_class


def test_diff_report_dataclass_repr_safe() -> None:
    report = DiffReport()
    repr(report)  # darf nicht crashen, auch wenn alles leer
