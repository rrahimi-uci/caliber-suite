"""Fetch OpenAPI documents for import.

Three sources, one exit: whatever the operator supplies becomes spec *text* here,
and :func:`caliber.integrations.openapi.normalize.parse_openapi_text` is the only
thing that interprets it.

Fetching a spec by URL is the feature's first SSRF surface — an operator pasting a
link is asking CALIBER to make an outbound request on their behalf, and
``http://169.254.169.254/`` is a URL. So the fetch goes through the same
:mod:`caliber.egress` policy as tool execution, with the same closed default.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from caliber.egress import EgressBlockedError, EgressPolicy, build_client, check_url

#: Ceiling on a fetched or uploaded document. Large public specs run to a few MB
#: (CALIBER's own is ~189 KB); 16 MB is generous while still bounding the parse and
#: the ``raw_document`` column that holds the result.
MAX_SPEC_BYTES = 16 * 1024 * 1024

#: Redirects are not followed — see ``caliber.egress`` — so a spec URL that 30x's
#: is reported rather than chased to an address the policy never vetted.
_FETCH_TIMEOUT_SECONDS = 30.0


class OpenApiLoadError(ValueError):
    """The supplied import source could not be turned into spec text."""


@dataclass(frozen=True)
class LoadedSpec:
    """Spec text plus the provenance CALIBER pins on the imported version."""

    spec_text: str
    source_kind: str
    source_ref: str
    content_type: str = ""
    byte_length: int = 0


def load_inline_spec(spec_text: str, *, source_ref: str = "") -> LoadedSpec:
    """Wrap pasted JSON/YAML text."""

    text = spec_text or ""
    encoded = text.encode("utf-8")
    if not text.strip():
        raise OpenApiLoadError("spec_text is required")
    if len(encoded) > MAX_SPEC_BYTES:
        raise OpenApiLoadError(
            f"spec is {len(encoded)} bytes, over the {MAX_SPEC_BYTES}-byte import limit"
        )
    return LoadedSpec(
        spec_text=text,
        source_kind="inline_text",
        source_ref=source_ref,
        byte_length=len(encoded),
    )


def load_uploaded_spec(spec_base64: str, *, source_ref: str = "") -> LoadedSpec:
    """Decode a base64-encoded uploaded file.

    Base64 rather than multipart because every other write route in the management
    API takes a JSON body, and a JSON-only contract keeps the SDK and ``client.raw``
    usable for this without a second code path.
    """

    raw = (spec_base64 or "").strip()
    if not raw:
        raise OpenApiLoadError("spec_base64 is required")
    # Cheap pre-check: base64 inflates by 4/3, so an over-limit payload is knowable
    # before decoding it into memory.
    if len(raw) // 4 * 3 > MAX_SPEC_BYTES:
        raise OpenApiLoadError(f"uploaded spec exceeds the {MAX_SPEC_BYTES}-byte import limit")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OpenApiLoadError(f"spec_base64 is not valid base64: {exc}") from exc
    if len(decoded) > MAX_SPEC_BYTES:
        raise OpenApiLoadError(f"uploaded spec exceeds the {MAX_SPEC_BYTES}-byte import limit")
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenApiLoadError(
            "uploaded spec is not UTF-8 text; OpenAPI documents must be JSON or YAML"
        ) from exc
    if not text.strip():
        raise OpenApiLoadError("uploaded spec is empty")
    return LoadedSpec(
        spec_text=text,
        source_kind="upload",
        source_ref=source_ref,
        byte_length=len(decoded),
    )


def load_spec_from_url(
    url: str,
    *,
    egress_policy: EgressPolicy | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = _FETCH_TIMEOUT_SECONDS,
) -> LoadedSpec:
    """Fetch a spec over HTTP under egress policy.

    ``egress_policy=None`` means the closed default, not "unrestricted": an
    unbound caller still cannot reach loopback, link-local, or private ranges.
    """

    target = (url or "").strip()
    if not target:
        raise OpenApiLoadError("spec_url is required")
    if not target.lower().startswith(("http://", "https://")):
        raise OpenApiLoadError(
            f"spec_url must be an http(s) URL: {target!r}"
        )
    policy = egress_policy if egress_policy is not None else EgressPolicy()
    try:
        check_url(target, policy)
    except EgressBlockedError as exc:
        raise OpenApiLoadError(str(exc)) from exc

    request_headers = {"Accept": "application/json, application/yaml, text/yaml, text/plain, */*"}
    request_headers.update({str(k): str(v) for k, v in (headers or {}).items()})
    try:
        with build_client(policy=policy, timeout=timeout) as client:
            response = client.get(target, headers=request_headers)
    except EgressBlockedError as exc:
        raise OpenApiLoadError(str(exc)) from exc
    except Exception as exc:
        raise OpenApiLoadError(f"could not fetch spec from {target!r}: {exc}") from exc

    if response.is_redirect:
        location = response.headers.get("location", "")
        raise OpenApiLoadError(
            f"spec_url returned a redirect to {location!r}; redirects are not followed — "
            "import the final URL directly"
        )
    if response.status_code >= 400:
        raise OpenApiLoadError(
            f"spec_url returned HTTP {response.status_code} for {target!r}"
        )
    content = response.content
    if len(content) > MAX_SPEC_BYTES:
        raise OpenApiLoadError(
            f"fetched spec is {len(content)} bytes, over the {MAX_SPEC_BYTES}-byte import limit"
        )
    try:
        text = content.decode(response.encoding or "utf-8", errors="strict")
    except (UnicodeDecodeError, LookupError) as exc:
        raise OpenApiLoadError(
            f"fetched spec from {target!r} is not decodable text"
        ) from exc
    if not text.strip():
        raise OpenApiLoadError(f"fetched spec from {target!r} is empty")
    return LoadedSpec(
        spec_text=text,
        source_kind="url",
        source_ref=target,
        content_type=str(response.headers.get("content-type") or ""),
        byte_length=len(content),
    )


def load_spec(
    *,
    source_kind: str,
    spec_text: str | None = None,
    spec_base64: str | None = None,
    spec_url: str | None = None,
    source_ref: str | None = None,
    egress_policy: EgressPolicy | None = None,
    fetch_headers: dict[str, str] | None = None,
) -> LoadedSpec:
    """Dispatch to the loader for ``source_kind``."""

    ref = source_ref or ""
    if source_kind == "inline_text":
        return load_inline_spec(spec_text or "", source_ref=ref)
    if source_kind == "upload":
        return load_uploaded_spec(spec_base64 or "", source_ref=ref)
    if source_kind == "url":
        return load_spec_from_url(
            spec_url or "",
            egress_policy=egress_policy,
            headers=fetch_headers,
        )
    raise OpenApiLoadError(
        f"unsupported source_kind {source_kind!r}; expected 'inline_text', 'upload', or 'url'"
    )


def probe_spec_source(
    *,
    source_kind: str,
    spec_url: str | None = None,
    egress_policy: EgressPolicy | None = None,
) -> dict[str, Any]:
    """Report whether an import source is reachable and permitted, without importing.

    Backs ``POST .../validate-spec-source`` so an operator can find out that a URL
    is blocked or unreachable before it becomes a failed import.
    """

    if source_kind != "url":
        return {
            "source_kind": source_kind,
            "reachable": True,
            "allowed": True,
            "detail": f"{source_kind} sources carry their own payload; nothing to reach",
        }
    target = (spec_url or "").strip()
    policy = egress_policy if egress_policy is not None else EgressPolicy()
    if not target.lower().startswith(("http://", "https://")):
        return {
            "source_kind": "url",
            "url": target,
            "reachable": False,
            "allowed": False,
            "detail": "spec_url must be an http(s) URL",
        }
    try:
        check_url(target, policy)
    except EgressBlockedError as exc:
        return {
            "source_kind": "url",
            "url": target,
            "reachable": False,
            "allowed": False,
            "detail": str(exc),
        }
    try:
        with build_client(policy=policy, timeout=_FETCH_TIMEOUT_SECONDS) as client:
            response = client.head(target)
            # Not every host answers HEAD; fall back rather than report a false negative.
            if response.status_code in (405, 501):
                response = client.get(target)
    except EgressBlockedError as exc:
        return {
            "source_kind": "url",
            "url": target,
            "reachable": False,
            "allowed": False,
            "detail": str(exc),
        }
    except Exception as exc:
        return {
            "source_kind": "url",
            "url": target,
            "reachable": False,
            "allowed": True,
            "detail": f"could not reach {target!r}: {exc}",
        }
    return {
        "source_kind": "url",
        "url": target,
        "reachable": response.status_code < 400,
        "allowed": True,
        "status_code": response.status_code,
        "content_type": str(response.headers.get("content-type") or ""),
        "detail": (
            "source is reachable"
            if response.status_code < 400
            else f"source returned HTTP {response.status_code}"
        ),
    }
