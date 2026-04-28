"""Unit-Tests fuer ``tests.cp_doc.diff.diff_openapi`` (AP-03).

Pflicht-Faelle laut Karte:

1. kein Drift
2. neuer Pfad (minor)
3. neuer 200-Field (minor)
4. entfernter 200-Field (breaking)
5. Typ-Aenderung (breaking)
6. neuer required Request-Field (breaking)
7. neuer optional Request-Field (minor)
8. Enum-Wert hinzu (minor)
9. Enum-Wert weg in Response (breaking)
10. description-Aenderung (value)
11. Status-Code 200 -> 201 (breaking)

Daneben einige defensive Faelle, die der Diff abdecken muss: entfernter
Pfad, entfernte Operation, neuer optional Request-Body-Field via OpenAPI 3.x
``requestBody``, removed enum value im Request (minor), gemischtes
breaking + minor (Klassifikation = breaking).
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tests.cp_doc.diff import SpecDiffReport, SpecFinding, diff_openapi


# --- Hilfen --------------------------------------------------------------


def _swagger_with(
    path: str,
    method: str,
    *,
    response_schema: dict[str, Any] | None = None,
    parameters: list[dict[str, Any]] | None = None,
    operation_extra: dict[str, Any] | None = None,
    status_code: str = "200",
) -> dict[str, Any]:
    """Baut eine minimale Swagger-2.0-Spec fuer Tests."""
    operation: dict[str, Any] = {
        "summary": "Test op",
        "description": "Test desc",
        "responses": {
            status_code: {
                "description": "Test response",
                "schema": response_schema or {"type": "object"},
            }
        },
    }
    if parameters is not None:
        operation["parameters"] = parameters
    if operation_extra:
        operation.update(operation_extra)
    return {
        "swagger": "2.0",
        "info": {"title": "Test", "version": "1.0"},
        "paths": {path: {method: operation}},
    }


def _classes(report: SpecDiffReport) -> list[tuple[str, str]]:
    """Hilfsfunktion: liste (kind, severity)-Paare aller Findings."""
    return [(f.kind, f.severity) for f in report.findings]


# --- Pflicht-Faelle ------------------------------------------------------


def test_no_drift_when_specs_identical() -> None:
    spec = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}},
    })
    report = diff_openapi(spec, deepcopy(spec))

    assert report.classification == "no drift"
    assert report.is_clean()


def test_added_path_is_minor() -> None:
    expected = _swagger_with("/x", "get")
    actual = deepcopy(expected)
    actual["paths"]["/y"] = {"get": {"responses": {"200": {"description": "ok"}}}}

    report = diff_openapi(actual, expected)

    assert report.classification == "minor (additive)"
    kinds = [f.kind for f in report.findings if f.severity == "minor"]
    assert "added_path" in kinds


def test_added_response_field_is_minor() -> None:
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object", "properties": {"a": {"type": "string"}},
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "minor (additive)"
    added = [f for f in report.findings if f.kind == "added_field"]
    assert len(added) == 1
    assert added[0].path.endswith(".properties.b")


def test_removed_response_field_is_breaking() -> None:
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object", "properties": {"a": {"type": "string"}},
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    removed = [f for f in report.findings if f.kind == "removed_field"]
    assert len(removed) == 1
    assert removed[0].severity == "breaking"
    assert removed[0].path.endswith(".properties.b")


def test_type_change_is_breaking() -> None:
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object", "properties": {"a": {"type": "string"}},
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object", "properties": {"a": {"type": "integer"}},
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    type_changes = [f for f in report.findings if f.kind == "type_changed"]
    assert len(type_changes) == 1
    assert type_changes[0].path.endswith(".properties.a.type")


def test_added_required_request_field_is_breaking() -> None:
    body_schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
    }
    expected = _swagger_with(
        "/x", "post",
        parameters=[{
            "in": "body", "name": "body", "required": True,
            "schema": body_schema,
        }],
    )
    actual = _swagger_with(
        "/x", "post",
        parameters=[{
            "in": "body", "name": "body", "required": True,
            "schema": {
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "integer"},
                },
                "required": ["b"],
            },
        }],
    )

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    breaking = [f for f in report.findings if f.severity == "breaking"]
    kinds = {f.kind for f in breaking}
    assert "added_required_field" in kinds


def test_added_optional_request_field_is_minor() -> None:
    expected = _swagger_with(
        "/x", "post",
        parameters=[{
            "in": "body", "name": "body", "required": True,
            "schema": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
            },
        }],
    )
    actual = _swagger_with(
        "/x", "post",
        parameters=[{
            "in": "body", "name": "body", "required": True,
            "schema": {
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "integer"},
                },
                # b ist NICHT in required[]
            },
        }],
    )

    report = diff_openapi(actual, expected)

    assert report.classification == "minor (additive)"
    added_optional = [
        f for f in report.findings if f.kind == "added_optional_field"
    ]
    assert len(added_optional) == 1
    assert added_optional[0].severity == "minor"


def test_enum_value_added_is_minor() -> None:
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"side": {"type": "string", "enum": ["BUY", "SELL"]}},
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {
            "side": {"type": "string", "enum": ["BUY", "SELL", "SHORT"]},
        },
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "minor (additive)"
    enum_added = [f for f in report.findings if f.kind == "enum_added"]
    assert len(enum_added) == 1
    assert enum_added[0].path.endswith(".enum+SHORT")


def test_enum_value_removed_in_response_is_breaking() -> None:
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {
            "side": {"type": "string", "enum": ["BUY", "SELL", "SHORT"]},
        },
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"side": {"type": "string", "enum": ["BUY", "SELL"]}},
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    enum_removed = [f for f in report.findings if f.kind == "enum_removed"]
    assert len(enum_removed) == 1
    assert enum_removed[0].severity == "breaking"


def test_description_change_is_value_only() -> None:
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string", "description": "alt"}},
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string", "description": "neu"}},
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "value (irrelevant)"
    value_findings = [f for f in report.findings if f.kind == "value_only"]
    assert len(value_findings) >= 1
    assert all(f.severity == "value" for f in value_findings)


def test_status_code_200_to_201_is_breaking() -> None:
    expected = _swagger_with("/x", "get", status_code="200")
    actual = _swagger_with("/x", "get", status_code="201")

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    removed_codes = [
        f for f in report.findings if f.kind == "removed_status_code"
    ]
    added_codes = [
        f for f in report.findings if f.kind == "added_status_code"
    ]
    assert len(removed_codes) == 1
    assert len(added_codes) == 1
    assert removed_codes[0].path.endswith(".200")
    assert added_codes[0].path.endswith(".201")


# --- Defensive Zusatzfaelle ----------------------------------------------


def test_removed_path_is_breaking() -> None:
    expected = _swagger_with("/x", "get")
    expected["paths"]["/y"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    actual = _swagger_with("/x", "get")

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    removed_paths = [f for f in report.findings if f.kind == "removed_path"]
    assert len(removed_paths) == 1
    assert removed_paths[0].path == "/y"


def test_removed_operation_is_breaking() -> None:
    expected = _swagger_with("/x", "get")
    expected["paths"]["/x"]["post"] = {
        "responses": {"200": {"description": "ok"}},
    }
    actual = _swagger_with("/x", "get")

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    removed_ops = [f for f in report.findings if f.kind == "removed_operation"]
    assert len(removed_ops) == 1
    assert removed_ops[0].path == "/x:post"


def test_added_operation_is_minor() -> None:
    expected = _swagger_with("/x", "get")
    actual = _swagger_with("/x", "get")
    actual["paths"]["/x"]["post"] = {
        "responses": {"200": {"description": "ok"}},
    }

    report = diff_openapi(actual, expected)

    assert report.classification == "minor (additive)"
    added_ops = [f for f in report.findings if f.kind == "added_operation"]
    assert len(added_ops) == 1
    assert added_ops[0].path == "/x:post"


def test_breaking_dominates_over_minor() -> None:
    # Aktuelles Spec hat ein neues Feld (minor) UND ein entferntes
    # Feld (breaking) im Response - Klassifikation muss breaking sein.
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}, "c": {"type": "boolean"}},
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    severities = {f.severity for f in report.findings}
    assert "breaking" in severities
    assert "minor" in severities


def test_required_added_in_response_schema_is_minor() -> None:
    # In Response: required hinzu = Server garantiert das Feld jetzt.
    # Konsumenten brechen nicht.
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}},
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "minor (additive)"
    req_added = [f for f in report.findings if f.kind == "required_added"]
    assert len(req_added) == 1
    assert req_added[0].severity == "minor"


def test_required_removed_in_response_schema_is_breaking() -> None:
    # In Response: required-Feld jetzt optional = Konsumenten brechen,
    # weil Wert fehlen darf.
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}},
    })

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    req_removed = [f for f in report.findings if f.kind == "required_removed"]
    assert len(req_removed) == 1
    assert req_removed[0].severity == "breaking"


def test_enum_value_removed_in_request_is_minor() -> None:
    expected = _swagger_with(
        "/x", "post",
        parameters=[{
            "in": "body", "name": "body", "required": True,
            "schema": {
                "type": "object",
                "properties": {
                    "side": {"type": "string", "enum": ["BUY", "SELL", "SHORT"]},
                },
            },
        }],
    )
    actual = _swagger_with(
        "/x", "post",
        parameters=[{
            "in": "body", "name": "body", "required": True,
            "schema": {
                "type": "object",
                "properties": {
                    "side": {"type": "string", "enum": ["BUY", "SELL"]},
                },
            },
        }],
    )

    report = diff_openapi(actual, expected)

    enum_removed = [f for f in report.findings if f.kind == "enum_removed"]
    assert len(enum_removed) == 1
    assert enum_removed[0].severity == "minor"


def test_query_parameter_type_change_is_breaking() -> None:
    expected = _swagger_with(
        "/x", "get",
        parameters=[{"in": "query", "name": "limit", "type": "string"}],
    )
    actual = _swagger_with(
        "/x", "get",
        parameters=[{"in": "query", "name": "limit", "type": "integer"}],
    )

    report = diff_openapi(actual, expected)

    assert report.classification == "breaking"
    type_changes = [f for f in report.findings if f.kind == "type_changed"]
    assert any("limit].type" in f.path for f in type_changes)


def test_render_markdown_contains_classification_and_path() -> None:
    expected = _swagger_with("/x", "get", response_schema={
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    })
    actual = _swagger_with("/x", "get", response_schema={
        "type": "object", "properties": {"a": {"type": "string"}},
    })

    report = diff_openapi(actual, expected)
    md = report.render_markdown(title="Drift /x")

    assert "Drift /x" in md
    assert "breaking" in md
    assert "removed_field" in md
    assert ".properties.b" in md
