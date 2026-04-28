"""Doku-Drift-Logik fuer OpenAPI-Spec-Vergleiche (AP-03).

Single Source of Truth fuer den Vergleich der eingecheckten IBKR-OpenAPI-
Snapshot (``docs/research/ibkr-cpapi-doc.json``) gegen die Live-Spec.
``scripts/check_doc_drift.py`` ruft ausschliesslich :func:`diff_openapi`
auf - keine Inline-Diffs.

Bewusst getrennt vom Live-Drift-Modul ``tests.cp_mock.diff``: dort werden
JSON-Antwort-Snapshots verglichen, hier die OpenAPI-Spec-Struktur. Die
Datenformate sind zu unterschiedlich, um eine gemeinsame Diff-Engine
sinnvoll zu machen.
"""
