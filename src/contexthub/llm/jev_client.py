"""TypeSafe's typed decisions with the research execution ledger and retry policy."""

import asyncio
from datetime import datetime, timezone
import math
import time

import httpx

from .retry_policy import ResponseParseError, ResponseSchemaError
from contexthub.models.execution import (
    AttemptError,
    AttemptRecord,
    Usage,
    calculate_cost,
)
from contexthub.models.knowledge import canonical_hash, text_hash

JEV_MODEL = "jev-1.13.0"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevClient:
    def __init__(self, api_key, *, transport=None):
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, transport=transport
        )

    async def evaluate(self, payload, *, call):
        for number in range(1, call.policy.max_attempts + 1):
            usage = Usage(
                completeness="missing", missing_reason="request_not_completed"
            )
            common = dict(
                identity=call.identity,
                attempt_no=number,
                requested_model=JEV_MODEL,
                input_sha256=canonical_hash(payload),
                prompt_sha256=canonical_hash(payload["questions"]),
                request_config_sha256=canonical_hash(
                    {"endpoint": JEV_ENDPOINT, "model": JEV_MODEL}
                ),
                retry_policy_sha256=canonical_hash(call.policy),
                started_at=datetime.now(timezone.utc),
                price=call.price,
            )
            await call.recorder.append(
                AttemptRecord(
                    **common,
                    phase="started",
                    outcome="started",
                    usage=usage,
                    actual_model_missing_reason="request_not_completed",
                    request_id_missing_reason="request_not_completed",
                    request_payload=payload,
                    cost=calculate_cost(usage, call.price, None),
                )
            )
            tick = time.monotonic()
            response = data = raw = error = probabilities = None
            try:
                timeout = call.policy.timeout_for_attempt(number)
                async with asyncio.timeout(timeout):
                    response = await self._client.post(
                        JEV_ENDPOINT, json=payload, timeout=timeout
                    )
                raw = response.text
                try:
                    data = response.json()
                except ValueError:
                    pass
                response.raise_for_status()
                if data is None:
                    raise ResponseParseError("response_json_invalid")
                probabilities = self._answers(data, payload["questions"])
            except (Exception, asyncio.CancelledError) as exc:
                error = exc
            usage = self._usage(data)
            model = data.get("model") if isinstance(data, dict) else None
            model = model if isinstance(model, str) and model.strip() else None
            request_id = (
                response.headers.get("x-request-id") if response is not None else None
            )
            request_id = request_id if request_id and request_id.strip() else None
            retryable = error is not None and call.policy.is_retryable(error)
            cancelled = isinstance(error, asyncio.CancelledError)
            again = retryable and number < call.policy.max_attempts
            stop = (
                "success"
                if error is None
                else "cancelled"
                if cancelled
                else "retry_scheduled"
                if again
                else "retry_exhausted"
                if retryable
                else "non_retryable"
            )
            await call.recorder.append(
                AttemptRecord(
                    **common,
                    phase="finished",
                    outcome="success"
                    if error is None
                    else "cancelled"
                    if cancelled
                    else "error",
                    actual_model=model,
                    actual_model_missing_reason=None
                    if model
                    else "provider_not_returned",
                    request_id=request_id,
                    request_id_missing_reason=None
                    if request_id
                    else "provider_not_returned",
                    raw_output=raw,
                    output_sha256=text_hash(raw) if raw is not None else None,
                    finished_at=datetime.now(timezone.utc),
                    wall_seconds=time.monotonic() - tick,
                    status_code=response.status_code if response is not None else None,
                    error=AttemptError(
                        code=f"http_{response.status_code}"
                        if isinstance(error, httpx.HTTPStatusError)
                        else type(error).__name__,
                        error_class=type(error).__name__,
                        retryable=retryable,
                    )
                    if error
                    else None,
                    stop_reason=stop,
                    usage=usage,
                    cost=calculate_cost(usage, call.price, model),
                )
            )
            if error is None:
                return probabilities
            if not again:
                raise error
            await call.sleep(call.policy.delay(number, error))

    @staticmethod
    def _answers(data, questions):
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise ResponseSchemaError("answers_must_match_all_supplied_candidates")
        def probability(value):
            return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1

        for ref, answer in answers.items():
            kind = questions[ref]["type"]
            if not isinstance(answer, dict) or answer.get("type") != kind:
                raise ResponseSchemaError("answer_type_mismatch")
            if kind == "noul":
                if not probability(answer.get("noul")):
                    raise ResponseSchemaError("noul_probability_out_of_range")
            elif kind == "choice":
                distribution = answer.get("probabilities")
                options = questions[ref]["criteria"]
                if (
                    not isinstance(distribution, dict)
                    or set(distribution) != set(options)
                    or not all(probability(p) for p in distribution.values())
                    # The API rounds each probability to two decimal places.
                    # Allow accumulated rounding without changing the returned values.
                    or not math.isclose(
                        sum(distribution.values()), 1,
                        abs_tol=0.005 * len(distribution) + 1e-9,
                    )
                    or not isinstance(answer.get("choice"), str)
                    or answer["choice"] not in options
                    or distribution[answer["choice"]] != max(distribution.values())
                    or not probability(answer.get("confidence"))
                ):
                    raise ResponseSchemaError("invalid_choice_answer")
            else:
                raise ResponseSchemaError("unsupported_question_type")
        return answers

    @staticmethod
    def _usage(data):
        raw = data.get("usage") if isinstance(data, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        counts = [raw.get(key) for key in ("input_tokens", "output_tokens")]
        inp, out = [v if type(v) is int and v >= 0 else None for v in counts]
        complete = inp is not None and out is not None
        return Usage(
            input_tokens=inp,
            output_tokens=out,
            total_tokens=inp + out if complete else None,
            completeness="complete"
            if complete
            else "partial"
            if inp is not None or out is not None
            else "missing",
            missing_reason=None if complete else "provider_usage_incomplete",
        )

    async def close(self):
        await self._client.aclose()
