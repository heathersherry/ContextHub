"""Call-local references and one correction attempt for invalid model output."""

import json

from contexthub.llm.retry_policy import ResponseParseError, ResponseSchemaError
from .contracts import prompt, ref_of

OUTPUT_ATTEMPTS = 2


class OutputValidationFailed(ValueError):
    """Model output remained invalid after the correction attempt."""


def timed_text(row):
    """Keep source order and recorded times distinct from event times in text."""
    return {k: row[k] for k in ("text", "created_in_session", "timestamp") if k in row}


class LocalReferences:
    def __init__(self, rows):
        refs = {ref_of(row) for row in rows}
        self.decode_map = {f"M{i}": ref for i, ref in enumerate(sorted(refs), 1)}
        self.encode_map = {ref: key for key, ref in self.decode_map.items()}
        ids = sorted({ref.rsplit(":", 1)[0] for ref in refs})
        self.matters = {uid: f"G{i}" for i, uid in enumerate(ids, 1)}

    def encode(self, ref):
        return self.encode_map[ref]

    def decode(self, key):
        if key not in self.decode_map:
            raise ValueError(
                f"Unknown memory reference {key!r}; use a supplied M identifier"
            )
        return self.decode_map[key]

    def describe(self, row):
        return {"ref": self.encode(ref_of(row)), **timed_text(row)}


async def validated_completion(
    client, context, *, stage, log_stage, rendered, retry, price, validate
):
    request = rendered
    for attempt in range(1, OUTPUT_ATTEMPTS + 1):
        call = context.make(
            stage, {"prompt": request}, retry, price=price, output_attempt=attempt
        )
        await context.store.record(
            context,
            log_stage + ".model_attempt",
            {
                "attempt": attempt,
                "call_id": str(call.identity.call_id),
                "prompt": request,
            },
        )
        raw = None
        try:
            raw = await client.complete(request, max_tokens=None, call=call)
        except (ResponseParseError, ResponseSchemaError) as exc:
            error = exc
            raw = client.last_raw_response
        else:
            try:
                return validate(raw)
            except ValueError as exc:
                error = exc
        # Transport, account, persistence errors and cancellation do not enter
        # this loop. No database mutations are performed by validate().
        await context.store.record(
            context,
            log_stage + ".validation_error",
            {
                "attempt": attempt,
                "call_id": str(call.identity.call_id),
                "error": str(error),
                "raw": raw,
                "retrying": attempt < OUTPUT_ATTEMPTS,
            },
        )
        if attempt == OUTPUT_ATTEMPTS:
            raise OutputValidationFailed(str(error)) from error
        feedback = json.dumps(
            {"validation_error": str(error), "previous_response": raw},
            ensure_ascii=False,
        )
        request = prompt("output_retry") + "\n" + feedback + "\n" + rendered
