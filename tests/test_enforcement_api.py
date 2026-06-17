from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from contexthub.api.deps import get_db, get_enforcement_service
from contexthub.api.routers.enforce import router as enforce_router
from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import (
    GuardrailDecision,
    Verdict,
    Violation,
    ViolationKind,
)


class StubEnforcementService:
    def __init__(self, decision: GuardrailDecision):
        self.decision = decision
        self.calls: list[tuple[object, EnforcementContext]] = []

    async def enforce(self, db, ec: EnforcementContext) -> GuardrailDecision:
        self.calls.append((db, ec))
        return self.decision


def _app_with(service: StubEnforcementService, db=object()) -> FastAPI:
    app = FastAPI()
    app.include_router(enforce_router)

    async def override_db():
        yield db

    def override_enforcement_service():
        return service

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_enforcement_service] = override_enforcement_service
    return app


def _headers() -> dict[str, str]:
    return {"X-Account-Id": "acme", "X-Agent-Id": "agent-a"}


def _post(client: TestClient, json: dict):
    return client.post("/api/v1/enforce", headers=_headers(), json=json)


def test_allow_verdict_passes_through():
    service = StubEnforcementService(
        GuardrailDecision(verdict=Verdict.ALLOW, reason="ok", guardrail="fake")
    )
    client = TestClient(_app_with(service))

    response = _post(client, {"boundary": "handoff"})

    assert response.status_code == 200
    assert response.json() == {
        "verdict": "allow",
        "reason": "ok",
        "guardrail": "fake",
        "violations": [],
        "sanitized_payload": None,
    }


def test_block_verdict_returns_200_with_violation():
    violation = Violation(
        kind=ViolationKind.INCOMPLETE_HANDOFF,
        message="missing task intent",
        repair_hint={"missing_fields": ["task_intent"]},
        evidence={"sender": "agent-a"},
    )
    service = StubEnforcementService(
        GuardrailDecision(
            verdict=Verdict.BLOCK,
            violations=[violation],
            reason="blocked",
            guardrail="handoff",
        )
    )
    client = TestClient(_app_with(service))

    response = _post(client, {"boundary": "handoff"})

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "block"
    assert len(body["violations"]) == 1
    assert body["violations"][0]["kind"] == "incomplete_handoff"
    assert body["violations"][0]["message"] == "missing task intent"


def test_repair_verdict_returns_sanitized_payload():
    sanitized = {"items": [{"uri": "ctx://team/case", "fields": {"ssn": "[REDACTED]"}}]}
    service = StubEnforcementService(
        GuardrailDecision(
            verdict=Verdict.REPAIR,
            reason="masked",
            guardrail="flow",
            sanitized_payload=sanitized,
        )
    )
    client = TestClient(_app_with(service))

    response = _post(client, {"boundary": "tool_call", "payload": {"items": []}})

    assert response.status_code == 200
    assert response.json()["verdict"] == "repair"
    assert response.json()["sanitized_payload"] == sanitized


def test_boundary_is_parsed_to_enum():
    service = StubEnforcementService(
        GuardrailDecision(verdict=Verdict.ALLOW, reason="ok", guardrail="fake")
    )
    client = TestClient(_app_with(service))

    response = _post(client, {"boundary": "handoff"})

    assert response.status_code == 200
    assert service.calls[0][1].boundary == Boundary.HANDOFF


def test_recipient_uses_actor_account_id():
    service = StubEnforcementService(
        GuardrailDecision(verdict=Verdict.ALLOW, reason="ok", guardrail="fake")
    )
    client = TestClient(_app_with(service))

    response = _post(
        client,
        {
            "boundary": "handoff",
            "recipient_agent_id": "agent-b",
        },
    )

    assert response.status_code == 200
    ec = service.calls[0][1]
    assert ec.actor.account_id == "acme"
    assert ec.actor.agent_id == "agent-a"
    assert ec.recipient is not None
    assert ec.recipient.account_id == "acme"
    assert ec.recipient.agent_id == "agent-b"


def test_missing_headers_returns_validation_error():
    service = StubEnforcementService(
        GuardrailDecision(verdict=Verdict.ALLOW, reason="ok", guardrail="fake")
    )
    client = TestClient(_app_with(service))

    response = client.post("/api/v1/enforce", json={"boundary": "handoff"})

    assert response.status_code == 422


def test_invalid_boundary_returns_validation_error():
    service = StubEnforcementService(
        GuardrailDecision(verdict=Verdict.ALLOW, reason="ok", guardrail="fake")
    )
    client = TestClient(_app_with(service))

    response = _post(client, {"boundary": "bogus"})

    assert response.status_code == 422
    assert service.calls == []


def test_approval_payload_is_preserved():
    service = StubEnforcementService(
        GuardrailDecision(verdict=Verdict.ALLOW, reason="ok", guardrail="closure")
    )
    client = TestClient(_app_with(service))
    payload = {
        "require_decision": True,
        "anchor": {"workflow_id": "wf-1", "required_actions": []},
    }

    response = _post(client, {"boundary": "closure", "payload": payload})

    assert response.status_code == 200
    assert service.calls[0][1].payload == payload
    assert service.calls[0][1].payload["require_decision"] is True


def test_main_app_openapi_includes_enforce_route():
    from contexthub.main import app

    assert "/api/v1/enforce" in app.openapi()["paths"]
