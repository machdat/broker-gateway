"""OpenAPI-/Swagger-Spec-Diff (AP-03 Doku-Drift-Detection).

Vergleicht zwei Spec-Strukturen (Swagger 2.0 oder OpenAPI 3.x, gleichermassen
behandelt) und klassifiziert die Drift in vier Stufen:

- **no drift** - keine relevante Aenderung.
- **minor (additive)** - neuer Pfad, neue Operation, neues optionales
  Request-Feld, neues Response-Feld, neuer Enum-Wert. Konsumenten brechen
  nicht.
- **breaking** - entfernter Pfad/Operation/Status-Code, entferntes Response-
  Feld, Typ-Aenderung, neues required Request-Feld, entfernter Enum-Wert
  in Response. Sofortige Eskalation noetig.
- **value (irrelevant)** - nur description/summary/example/externalDocs
  geaendert. Kein Verhalten geaendert.

Ignoriert wird laut Karte: x-* Vendor-Extensions sowie die Doku-Felder
``description``/``summary``/``example``/``externalDocs`` (sie werden zwar
erkannt und einem Finding mit Severity ``value`` zugeordnet, sind aber
nie breaking).

Die Funktion macht **keine** $ref-Aufloesung. Wenn beide Seiten dieselbe
$ref haben, liefert das einen sauberen "no drift"-Befund. Wenn $ref
abweicht, wird das als ``type_changed`` gemeldet - das ist konservativ
korrekt (eine geaenderte Referenz ist eine Schemaaenderung).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal


Severity = Literal["no", "value", "minor", "breaking"]
SchemaMode = Literal["request", "response", "neither"]


_HTTP_METHODS: tuple[str, ...] = (
    "get", "post", "put", "delete", "patch", "head", "options",
)

# Felder, deren Aenderung nur "value drift" sind - keine Verhaltensaenderung.
_VALUE_ONLY_FIELDS: tuple[str, ...] = (
    "description", "summary", "example", "externalDocs",
)


@dataclass
class SpecFinding:
    """Ein einzelner Drift-Befund mit Pfad, Art und Severity."""

    path: str
    kind: str
    severity: Severity
    detail: str = ""

    def is_breaking(self) -> bool:
        return self.severity == "breaking"

    def is_minor(self) -> bool:
        return self.severity == "minor"

    def is_value(self) -> bool:
        return self.severity == "value"


@dataclass
class SpecDiffReport:
    """Sammelt alle Findings eines OpenAPI-Diffs und klassifiziert das Gesamtbild."""

    findings: list[SpecFinding] = field(default_factory=list)

    def add(self, finding: SpecFinding) -> None:
        self.findings.append(finding)

    # Gesamtklassifikation. Ein einziges breaking schlaegt alles. Sonst minor,
    # sonst value, sonst no drift.

    def is_clean(self) -> bool:
        return not self.findings

    def is_breaking(self) -> bool:
        return any(f.is_breaking() for f in self.findings)

    def is_minor(self) -> bool:
        if self.is_breaking():
            return False
        return any(f.is_minor() for f in self.findings)

    def is_value_only(self) -> bool:
        if self.is_breaking() or self.is_minor():
            return False
        return any(f.is_value() for f in self.findings)

    @property
    def classification(self) -> str:
        if self.is_breaking():
            return "breaking"
        if self.is_minor():
            return "minor (additive)"
        if self.is_value_only():
            return "value (irrelevant)"
        return "no drift"

    def breaking_findings(self) -> list[SpecFinding]:
        return [f for f in self.findings if f.is_breaking()]

    def minor_findings(self) -> list[SpecFinding]:
        return [f for f in self.findings if f.is_minor()]

    def value_findings(self) -> list[SpecFinding]:
        return [f for f in self.findings if f.is_value()]

    def render_markdown(self, *, title: str = "OpenAPI-Doku-Drift") -> str:
        lines: list[str] = [f"# {title}", "", f"**Klassifikation:** {self.classification}", ""]
        if self.is_clean():
            lines.append("- no drift")
            return "\n".join(lines) + "\n"

        if self.breaking_findings():
            lines.append("## Breaking")
            for f in self.breaking_findings():
                lines.append(f"- `{f.path}` ({f.kind}){_with_detail(f)}")
            lines.append("")
        if self.minor_findings():
            lines.append("## Minor (additive)")
            for f in self.minor_findings():
                lines.append(f"- `{f.path}` ({f.kind}){_with_detail(f)}")
            lines.append("")
        if self.value_findings():
            lines.append("## Value (irrelevant)")
            for f in self.value_findings():
                lines.append(f"- `{f.path}` ({f.kind}){_with_detail(f)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def diff_openapi(actual_spec: dict[str, Any], expected_spec: dict[str, Any]) -> SpecDiffReport:
    """Vergleicht zwei OpenAPI-/Swagger-Specs und liefert einen SpecDiffReport.

    Args:
        actual_spec: Frische Spec (z.B. live von IBKR gefetcht).
        expected_spec: Eingecheckte Baseline (``docs/research/ibkr-cpapi-doc.json``).
    """
    report = SpecDiffReport()
    actual_paths: dict[str, Any] = (actual_spec.get("paths") or {})
    expected_paths: dict[str, Any] = (expected_spec.get("paths") or {})

    a_keys = set(actual_paths.keys())
    e_keys = set(expected_paths.keys())

    for path in sorted(a_keys - e_keys):
        report.add(SpecFinding(
            path=path, kind="added_path", severity="minor",
            detail="Neuer Pfad in Live-Spec - additive.",
        ))
    for path in sorted(e_keys - a_keys):
        report.add(SpecFinding(
            path=path, kind="removed_path", severity="breaking",
            detail="Pfad nicht mehr in Live-Spec - Konsument bricht.",
        ))
    for path in sorted(a_keys & e_keys):
        _diff_path_item(actual_paths[path], expected_paths[path], path, report)

    return report


# ---- Internals ----


def _diff_path_item(
    actual: dict[str, Any],
    expected: dict[str, Any],
    path: str,
    report: SpecDiffReport,
) -> None:
    for method in _HTTP_METHODS:
        a_op = actual.get(method)
        e_op = expected.get(method)
        if a_op is None and e_op is None:
            continue
        op_path = f"{path}:{method}"
        if e_op is None:
            report.add(SpecFinding(
                path=op_path, kind="added_operation", severity="minor",
                detail=f"Neue HTTP-Methode {method.upper()} unter {path}.",
            ))
            continue
        if a_op is None:
            report.add(SpecFinding(
                path=op_path, kind="removed_operation", severity="breaking",
                detail=f"HTTP-Methode {method.upper()} unter {path} entfernt.",
            ))
            continue
        _diff_operation(a_op, e_op, path, method, report)


def _diff_operation(
    actual: dict[str, Any],
    expected: dict[str, Any],
    path: str,
    method: str,
    report: SpecDiffReport,
) -> None:
    op_path = f"{path}:{method}"

    # Description / summary - reine value drift.
    for key in _VALUE_ONLY_FIELDS:
        if actual.get(key) != expected.get(key):
            report.add(SpecFinding(
                path=f"{op_path}.{key}", kind="value_only", severity="value",
                detail=f"{key} geaendert - kein Verhaltensbruch.",
            ))

    _diff_parameters(
        actual.get("parameters") or [],
        expected.get("parameters") or [],
        op_path,
        report,
    )
    _diff_responses(
        actual.get("responses") or {},
        expected.get("responses") or {},
        op_path,
        report,
    )

    # OpenAPI 3.x: requestBody. Swagger 2.0: ueber parameters mit in:body.
    a_rb = actual.get("requestBody")
    e_rb = expected.get("requestBody")
    if a_rb is not None or e_rb is not None:
        _diff_request_body(a_rb or {}, e_rb or {}, f"{op_path}.requestBody", report)


def _diff_parameters(
    actual: Iterable[dict[str, Any]],
    expected: Iterable[dict[str, Any]],
    op_path: str,
    report: SpecDiffReport,
) -> None:
    a_map = {_param_key(p): p for p in actual}
    e_map = {_param_key(p): p for p in expected}

    for key in sorted(set(a_map) - set(e_map)):
        param = a_map[key]
        is_required = bool(param.get("required"))
        kind = "added_required_parameter" if is_required else "added_optional_parameter"
        severity: Severity = "breaking" if is_required else "minor"
        report.add(SpecFinding(
            path=f"{op_path}.parameters[{key[0]}/{key[1]}]",
            kind=kind, severity=severity,
            detail=f"Parameter {key[1]} ({key[0]}) hinzugekommen, required={is_required}.",
        ))

    for key in sorted(set(e_map) - set(a_map)):
        param = e_map[key]
        is_required = bool(param.get("required"))
        # Required-Parameter entfernt = breaking (Konsument schickt
        # ueberfluessigen, ggf. fehlerhaft validierten Wert), optional
        # entfernt = minor (Server ignoriert den ueberfluessigen Input).
        kind = "removed_required_parameter" if is_required else "removed_optional_parameter"
        severity = "breaking" if is_required else "minor"
        report.add(SpecFinding(
            path=f"{op_path}.parameters[{key[0]}/{key[1]}]",
            kind=kind, severity=severity,
            detail=f"Parameter {key[1]} ({key[0]}) entfernt, required={is_required}.",
        ))

    for key in sorted(set(a_map) & set(e_map)):
        _diff_parameter(
            a_map[key], e_map[key],
            f"{op_path}.parameters[{key[0]}/{key[1]}]",
            report,
        )


def _diff_parameter(
    actual: dict[str, Any],
    expected: dict[str, Any],
    base: str,
    report: SpecDiffReport,
) -> None:
    # required-flag: false -> true ist breaking (Konsument muss Feld jetzt
    # immer mitschicken). true -> false ist minor (rueckwaertskompatibel).
    a_req = bool(actual.get("required"))
    e_req = bool(expected.get("required"))
    if not e_req and a_req:
        report.add(SpecFinding(
            path=f"{base}.required", kind="required_added", severity="breaking",
            detail="Parameter wurde required - Konsument bricht ohne Update.",
        ))
    if e_req and not a_req:
        report.add(SpecFinding(
            path=f"{base}.required", kind="required_removed", severity="minor",
            detail="Parameter ist nicht mehr required - rueckwaertskompatibel.",
        ))

    # Type direkt am Parameter (Swagger 2.0 fuer non-body-params).
    a_t = actual.get("type")
    e_t = expected.get("type")
    if a_t is not None and e_t is not None and a_t != e_t:
        report.add(SpecFinding(
            path=f"{base}.type", kind="type_changed", severity="breaking",
            detail=f"Typ {e_t} -> {a_t}.",
        ))

    # Description - value drift.
    if actual.get("description") != expected.get("description"):
        report.add(SpecFinding(
            path=f"{base}.description", kind="value_only", severity="value",
        ))

    # Enum am Parameter selbst (Swagger 2.0).
    _diff_enum(actual, expected, base, report, mode="request")

    # Schema-Block bei body-Parametern.
    if "schema" in actual or "schema" in expected:
        _diff_schema(
            actual.get("schema") or {}, expected.get("schema") or {},
            f"{base}.schema", report, mode="request",
        )


def _diff_responses(
    actual: dict[str, Any],
    expected: dict[str, Any],
    op_path: str,
    report: SpecDiffReport,
) -> None:
    a_codes = set(_str_keys(actual))
    e_codes = set(_str_keys(expected))

    for code in sorted(a_codes - e_codes):
        # Neuer Status-Code in Live-Spec. Wenn es ein neuer Erfolg-Code
        # ist (2xx) und der alte 2xx-Code nicht mehr existiert, faellt
        # das ueber removed_status_code als breaking auf - daher hier
        # einfach nur additive markieren.
        report.add(SpecFinding(
            path=f"{op_path}.responses.{code}",
            kind="added_status_code", severity="minor",
            detail=f"Neuer Response-Status {code}.",
        ))

    for code in sorted(e_codes - a_codes):
        report.add(SpecFinding(
            path=f"{op_path}.responses.{code}",
            kind="removed_status_code", severity="breaking",
            detail=f"Response-Status {code} entfernt - Konsumenten brechen.",
        ))

    for code in sorted(a_codes & e_codes):
        a_resp = actual[code] or {}
        e_resp = expected[code] or {}
        base = f"{op_path}.responses.{code}"

        for key in _VALUE_ONLY_FIELDS:
            if a_resp.get(key) != e_resp.get(key):
                report.add(SpecFinding(
                    path=f"{base}.{key}", kind="value_only", severity="value",
                ))

        # Swagger 2.0: response.schema. OpenAPI 3.x: response.content.{mediaType}.schema.
        if "schema" in a_resp or "schema" in e_resp:
            _diff_schema(
                a_resp.get("schema") or {}, e_resp.get("schema") or {},
                f"{base}.schema", report, mode="response",
            )
        if "content" in a_resp or "content" in e_resp:
            _diff_content(
                a_resp.get("content") or {}, e_resp.get("content") or {},
                f"{base}.content", report, mode="response",
            )


def _diff_request_body(
    actual: dict[str, Any],
    expected: dict[str, Any],
    base: str,
    report: SpecDiffReport,
) -> None:
    # required-flag des requestBody.
    a_req = bool(actual.get("required"))
    e_req = bool(expected.get("required"))
    if not e_req and a_req:
        report.add(SpecFinding(
            path=f"{base}.required", kind="required_added", severity="breaking",
        ))
    if e_req and not a_req:
        report.add(SpecFinding(
            path=f"{base}.required", kind="required_removed", severity="minor",
        ))

    if "content" in actual or "content" in expected:
        _diff_content(
            actual.get("content") or {}, expected.get("content") or {},
            f"{base}.content", report, mode="request",
        )


def _diff_content(
    actual: dict[str, Any],
    expected: dict[str, Any],
    base: str,
    report: SpecDiffReport,
    *,
    mode: SchemaMode,
) -> None:
    a_keys = set(actual.keys())
    e_keys = set(expected.keys())

    for media in sorted(a_keys - e_keys):
        report.add(SpecFinding(
            path=f"{base}.{media}", kind="added_media_type", severity="minor",
        ))
    for media in sorted(e_keys - a_keys):
        report.add(SpecFinding(
            path=f"{base}.{media}", kind="removed_media_type", severity="breaking",
        ))

    for media in sorted(a_keys & e_keys):
        a_block = actual[media] or {}
        e_block = expected[media] or {}
        _diff_schema(
            a_block.get("schema") or {}, e_block.get("schema") or {},
            f"{base}.{media}.schema", report, mode=mode,
        )


def _diff_schema(
    actual: dict[str, Any] | None,
    expected: dict[str, Any] | None,
    base: str,
    report: SpecDiffReport,
    *,
    mode: SchemaMode,
) -> None:
    actual = actual or {}
    expected = expected or {}

    # $ref: konservativ vergleichen, nicht aufloesen. Aenderung der
    # Referenz wird als type_changed gemeldet.
    a_ref = actual.get("$ref")
    e_ref = expected.get("$ref")
    if a_ref != e_ref and (a_ref is not None or e_ref is not None):
        report.add(SpecFinding(
            path=f"{base}.$ref", kind="type_changed", severity="breaking",
            detail=f"$ref {e_ref} -> {a_ref}.",
        ))
        # Nicht weiter rein - der $ref ist die ganze Definition.
        return

    # type-Aenderung an Schema (string/object/array/integer/...).
    a_t = actual.get("type")
    e_t = expected.get("type")
    if a_t is not None and e_t is not None and a_t != e_t:
        report.add(SpecFinding(
            path=f"{base}.type", kind="type_changed", severity="breaking",
            detail=f"Typ {e_t} -> {a_t}.",
        ))
        return

    # Format kann breaking sein (int32 -> int64 ist ok, aber string -> date
    # ist semantisch). Wir behandeln Format-Aenderung als breaking, weil
    # Konsumenten oft auf Format-Garantien (UUID, datetime) bauen.
    a_f = actual.get("format")
    e_f = expected.get("format")
    if a_f is not None and e_f is not None and a_f != e_f:
        report.add(SpecFinding(
            path=f"{base}.format", kind="type_changed", severity="breaking",
            detail=f"format {e_f} -> {a_f}.",
        ))

    # Doku-Felder als value drift markieren.
    for key in _VALUE_ONLY_FIELDS:
        if actual.get(key) != expected.get(key):
            report.add(SpecFinding(
                path=f"{base}.{key}", kind="value_only", severity="value",
            ))

    # Properties-Diff.
    a_props: dict[str, Any] = actual.get("properties") or {}
    e_props: dict[str, Any] = expected.get("properties") or {}
    a_required: set[str] = set(actual.get("required") or [])
    e_required: set[str] = set(expected.get("required") or [])

    for prop in sorted(set(a_props) - set(e_props)):
        is_required_now = prop in a_required
        kind, severity = _classify_added_field(mode, is_required_now)
        report.add(SpecFinding(
            path=f"{base}.properties.{prop}",
            kind=kind, severity=severity,
            detail=f"Feld {prop} hinzugekommen, required={is_required_now}.",
        ))

    for prop in sorted(set(e_props) - set(a_props)):
        was_required = prop in e_required
        kind, severity = _classify_removed_field(mode, was_required)
        report.add(SpecFinding(
            path=f"{base}.properties.{prop}",
            kind=kind, severity=severity,
            detail=f"Feld {prop} entfernt, war required={was_required}.",
        ))

    # required-Aenderung an common Properties.
    common_props = set(a_props) & set(e_props)
    for prop in sorted(a_required - e_required):
        if prop in common_props:
            kind = "required_added"
            severity_req: Severity = "breaking" if mode == "request" else "minor"
            report.add(SpecFinding(
                path=f"{base}.required+{prop}",
                kind=kind, severity=severity_req,
                detail=f"Feld {prop} ist jetzt required ({mode}).",
            ))
    for prop in sorted(e_required - a_required):
        if prop in common_props:
            kind = "required_removed"
            severity_loose: Severity = "minor" if mode == "request" else "breaking"
            report.add(SpecFinding(
                path=f"{base}.required-{prop}",
                kind=kind, severity=severity_loose,
                detail=f"Feld {prop} ist nicht mehr required ({mode}).",
            ))

    # Common Properties rekursiv.
    for prop in sorted(common_props):
        _diff_schema(
            a_props[prop], e_props[prop],
            f"{base}.properties.{prop}", report, mode=mode,
        )

    # Array-items.
    if "items" in actual or "items" in expected:
        _diff_schema(
            actual.get("items"), expected.get("items"),
            f"{base}.items", report, mode=mode,
        )

    # Enum-Diff.
    _diff_enum(actual, expected, base, report, mode=mode)


def _diff_enum(
    actual: dict[str, Any],
    expected: dict[str, Any],
    base: str,
    report: SpecDiffReport,
    *,
    mode: SchemaMode,
) -> None:
    a_enum = actual.get("enum")
    e_enum = expected.get("enum")
    if a_enum is None and e_enum is None:
        return
    if a_enum is None or e_enum is None:
        # Eine Seite hat enum, andere nicht. Asymmetrie: enum entfernt ist
        # additive (Server akzeptiert mehr Werte), enum hinzu ist breaking
        # in Response (Konsument bekommt evtl. unbekannte Werte) bzw.
        # breaking in Request (Konsument darf nur noch enum-Werte schicken).
        if e_enum is None and a_enum is not None:
            severity_added: Severity = "breaking" if mode == "request" else "minor"
            report.add(SpecFinding(
                path=f"{base}.enum", kind="enum_added_constraint",
                severity=severity_added,
                detail=f"enum-Constraint neu gesetzt ({sorted(map(str, a_enum))}).",
            ))
        else:
            report.add(SpecFinding(
                path=f"{base}.enum", kind="enum_removed_constraint",
                severity="minor",
                detail="enum-Constraint entfernt - mehr Werte erlaubt.",
            ))
        return

    a_set = {_hashable(v) for v in a_enum}
    e_set = {_hashable(v) for v in e_enum}

    for value in sorted(a_set - e_set, key=str):
        report.add(SpecFinding(
            path=f"{base}.enum+{value}", kind="enum_added", severity="minor",
            detail=f"Neuer Enum-Wert {value!r}.",
        ))

    for value in sorted(e_set - a_set, key=str):
        severity_enum: Severity = "breaking" if mode == "response" else "minor"
        report.add(SpecFinding(
            path=f"{base}.enum-{value}", kind="enum_removed", severity=severity_enum,
            detail=f"Enum-Wert {value!r} entfernt ({mode}).",
        ))


def _classify_added_field(mode: SchemaMode, is_required_now: bool) -> tuple[str, Severity]:
    if mode == "request":
        if is_required_now:
            return "added_required_field", "breaking"
        return "added_optional_field", "minor"
    # Response oder neither: neues Feld ist additive.
    return "added_field", "minor"


def _classify_removed_field(mode: SchemaMode, was_required: bool) -> tuple[str, Severity]:
    if mode == "request":
        # Server erwartet das Feld nicht mehr - Konsument darf es weiter senden,
        # wird ignoriert.
        return "removed_field", "minor"
    # Response: entferntes Feld bricht Konsumenten, die es lesen.
    return "removed_field", "breaking"


def _param_key(param: dict[str, Any]) -> tuple[str, str]:
    return (str(param.get("in", "")), str(param.get("name", "")))


def _str_keys(d: dict[Any, Any]) -> list[str]:
    return [str(k) for k in d.keys()]


def _hashable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return repr(v)


def _with_detail(finding: SpecFinding) -> str:
    return f": {finding.detail}" if finding.detail else ""
