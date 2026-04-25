"""Vereinheitlichtes Error-Modell fuer die v1-API.

Section 1.6 des v1-Contracts: jede Fehlerantwort hat das Schema

    { "error": { "code": "...", "message": "...", "request_id": "...",
                 "retry_after_s": 30, "extra": { ... } } }

Wo `code` ein Snake-Case-String aus :data:`KNOWN_ERROR_CODES` ist und
`request_id` aus dem ObservabilityMiddleware uebernommen wird, falls
vorhanden. `extra` ist endpunkt-spezifisch.

Endpoints werfen weiter `HTTPException`; der globale Handler in
:mod:`broker_gateway.main` uebersetzt das in dieses Schema. Wer einen
spezifischen `code` setzen will, gibt ein Dict als ``detail`` mit:

    raise HTTPException(status_code=403,
                        detail={"code": "missing_scope", "message": "...",
                                "extra": {"required_scope": "orders:write"}})
"""
from __future__ import annotations

from typing import Any, Mapping

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class ErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: str | None = None
    retry_after_s: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorPayload


# Single Source of Truth fuer Error-Codes pro Default-Status. Endpoints
# duerfen ueberschreiben, indem sie ``detail={"code": ..., ...}`` setzen.
_DEFAULT_CODE_BY_STATUS: dict[int, str] = {
    400: "invalid_input",
    401: "missing_token",
    403: "missing_scope",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    415: "unsupported_media",
    422: "invalid_input",
    429: "cp_pacing_violation",
    500: "internal_error",
    502: "cp_upstream_error",
    503: "auth_lost",
    504: "cp_timeout",
}


KNOWN_ERROR_CODES: frozenset[str] = frozenset({
    "invalid_input",
    "missing_token",
    "invalid_token",
    "missing_scope",
    "not_found",
    "method_not_allowed",
    "conflict",
    "idempotency_replay_mismatch",
    "unsupported_media",
    "cp_pacing_violation",
    "cp_upstream_error",
    "cp_timeout",
    "auth_lost",
    "internal_error",
})


def default_code_for(status_code: int) -> str:
    return _DEFAULT_CODE_BY_STATUS.get(status_code, f"http_{status_code}")


def build_error_response(
    *,
    status_code: int,
    code: str | None = None,
    message: str,
    request_id: str | None = None,
    retry_after_s: int | None = None,
    extra: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorPayload(
        code=code or default_code_for(status_code),
        message=message,
        request_id=request_id,
        retry_after_s=retry_after_s,
        extra=dict(extra or {}),
    )
    body = payload.model_dump(exclude_none=True)
    if not body.get("extra"):
        body.pop("extra", None)
    out_headers = dict(headers or {})
    if retry_after_s is not None and "Retry-After" not in out_headers:
        out_headers["Retry-After"] = str(retry_after_s)
    return JSONResponse(
        status_code=status_code,
        content={"error": body},
        headers=out_headers,
    )


class CPGatewayError(Exception):
    """Upstream-Fehler des CP-Gateways, der nicht 1:1 nach aussen darf.

    Der globale Handler in :mod:`broker_gateway.main` uebersetzt diese
    Exception in das vereinheitlichte Error-Schema mit ``code='cp_upstream_error'``.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        upstream_status: int | None = None,
        upstream_code: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code
        self.retry_after_s = retry_after_s
