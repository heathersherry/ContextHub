"""Opt-in durable HTTP evidence, written before semantic/schema validation.

Never stores request headers. Only allowlisted response metadata is retained.
Archive write failures are non-retryable: do not repeat a completed API call.
"""
import hashlib
import json
import os
from pathlib import Path

from contexthub.models.knowledge import ContractError


def archive_response(directory, *, identity, attempt_no, request, response, api_key):
    root = Path(directory)
    if not root.is_absolute():
        raise ContractError("response_archive_absolute_path_required")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{identity.call_id}-{attempt_no}.json"
    evidence = {
        "version": "http-response-archive-v1",
        "identity": identity.model_dump(mode="json"),
        "attempt_no": attempt_no,
        "request": request,
        "status_code": response.status_code,
        "response_headers": {
            k: v for k, v in response.headers.items()
            if k.lower() in {"content-type", "date", "x-request-id", "x-oneapi-request-id"}
        },
        "response_text": response.text,
        "credential_redacted": False,
    }
    # Redact the configured credential even if the provider echoes it in a body.
    def scrub(value):
        if isinstance(value, str):
            return value.replace(api_key, "[REDACTED]") if api_key else value
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, dict):
            return {scrub(k): scrub(v) for k, v in value.items()}
        return value
    cleaned = scrub(evidence)
    cleaned["credential_redacted"] = cleaned != evidence
    data = (json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    # A partial write on storage failure is kept for inspection, never overwritten.
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return {
        "response_archive_path": str(path),
        "response_archive_sha256": hashlib.sha256(data).hexdigest(),
    }
