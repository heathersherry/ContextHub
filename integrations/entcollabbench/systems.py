"""System-condition builders for EntCollabBench evaluation.

S1 is deliberately a generic static-control guardrail. It only validates
EntCollabBench-native packet/tool/output shape and never reads ContextHub ACL,
staleness, provenance, loaded world, or ContextHub guardrail implementations.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import (
    GuardrailDecision,
    Verdict,
    Violation,
    ViolationKind,
)
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.guardrails.tool_state import ToolStateGuardrail
from contexthub.enforcement.staleness import StalenessChecker
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService

from integrations.entcollabbench import mapping
from integrations.entcollabbench.interceptor import EnforcementInterceptor
from integrations.entcollabbench.metrics import InstanceResult
from integrations.entcollabbench.world_loader import LoadedWorld


@dataclass
class System:
    """Uniform wrapper for one evaluation system condition."""

    name: str
    guardrails: list[Any] = field(default_factory=list)
    interceptor: EnforcementInterceptor | None = None
    runner: Callable[..., Any] | None = None

    async def run_instance(
        self,
        instance: Mapping[str, Any],
        model: str,
        *,
        seed: int = 0,
        subset: str = "workflow",
    ) -> InstanceResult:
        """Run one instance via an injected EntCollabBench runner or fixture data."""

        native = await self._run_native(instance, model, seed=seed, subset=subset)
        result = _coerce_result(
            native,
            instance=instance,
            model=model,
            system=self.name,
            seed=seed,
            subset=subset,
        )

        if self.name in {"S1", "S1p"} and self.guardrails:
            result.guardrail_events.extend(
                await _evaluate_static_events(instance, self.guardrails[0])
            )
        return result

    async def _run_native(
        self,
        instance: Mapping[str, Any],
        model: str,
        *,
        seed: int,
        subset: str,
    ) -> Any:
        runner = self.runner or instance.get("runner")
        if runner is None:
            return dict(instance)

        value = runner(instance=instance, model=model, system=self, seed=seed, subset=subset)
        if inspect.isawaitable(value):
            return await value
        return value


class GenericGuardrail:
    """S1 fixed generic guardrail from Task 9 §4.1.1."""

    name = "generic"
    applies_to = frozenset({Boundary.HANDOFF, Boundary.TOOL_CALL, Boundary.CLOSURE})

    async def check(self, db, ec: EnforcementContext) -> GuardrailDecision:
        if ec.boundary == Boundary.HANDOFF:
            return self._check_handoff(ec.payload or {})
        if ec.boundary == Boundary.TOOL_CALL:
            return self._check_tool(ec.payload or {})
        if ec.boundary == Boundary.CLOSURE:
            return self._check_closure(ec.payload or {})
        return GuardrailDecision(Verdict.ALLOW, reason="boundary not applicable", guardrail=self.name)

    def _check_handoff(self, payload: Mapping[str, Any]) -> GuardrailDecision:
        handoff_keys = {"sender", "recipient", "task_intent", "expected_action"}
        if not any(key in payload for key in handoff_keys):
            return GuardrailDecision(Verdict.ALLOW, reason="no structured handoff", guardrail=self.name)

        missing = sorted(key for key in handoff_keys if _empty(payload.get(key)))
        if not missing:
            return GuardrailDecision(Verdict.ALLOW, reason="generic handoff complete", guardrail=self.name)
        return _decision(
            Verdict.REPAIR,
            ViolationKind.INCOMPLETE_HANDOFF,
            f"generic handoff missing required fields: {missing}",
            {"missing_fields": missing},
        )

    def _check_tool(self, payload: Mapping[str, Any]) -> GuardrailDecision:
        tool_name = _tool_name(payload)
        allowed_tools = _allowed_tools(payload)
        if allowed_tools and tool_name not in allowed_tools:
            return _decision(
                Verdict.BLOCK,
                ViolationKind.SCHEMA_OR_ENUM,
                f"tool {tool_name!r} is not in EntCollabBench allowlist",
                {"tool": tool_name, "allowed_tools": sorted(allowed_tools)},
            )

        schema = _tool_schema(payload)
        violations = _validate_args(schema, _tool_args(payload), tool_name or "<unknown>")
        if not violations:
            return GuardrailDecision(Verdict.ALLOW, reason="generic tool schema valid", guardrail=self.name)
        return GuardrailDecision(
            Verdict.REPAIR,
            violations=violations,
            reason="generic tool schema repair",
            guardrail=self.name,
        )

    def _check_closure(self, payload: Mapping[str, Any]) -> GuardrailDecision:
        final_output = (
            payload.get("final_output")
            or payload.get("output")
            or payload.get("message")
            or payload.get("text")
        )
        if "anchor" in payload and any(
            key in payload for key in ("completed_actions", "decision_label", "open_questions")
        ):
            final_output = final_output if final_output is not None else payload

        violations: list[Violation] = []
        if _empty(final_output):
            violations.append(
                Violation(
                    ViolationKind.UNCLOSED_WORKFLOW,
                    "generic closure output is empty",
                    repair_hint={"missing": "final_output"},
                )
            )

        allowed_labels = set(payload.get("allowed_decision_labels") or payload.get("allowed_labels") or [])
        decision_label = payload.get("decision_label") or payload.get("decision")
        if allowed_labels and decision_label not in allowed_labels:
            violations.append(
                Violation(
                    ViolationKind.WEAK_DECISION,
                    f"decision label {decision_label!r} not in allowed labels",
                    repair_hint={
                        "decision_label": decision_label,
                        "allowed_labels": sorted(allowed_labels),
                    },
                )
            )

        if not violations:
            return GuardrailDecision(Verdict.ALLOW, reason="generic closure valid", guardrail=self.name)
        return GuardrailDecision(
            Verdict.REPAIR,
            violations=violations,
            reason="generic closure repair",
            guardrail=self.name,
        )


class PolicyOnlyGuardrail:
    """Optional S1p static allow/deny action table."""

    name = "policy_only"
    applies_to = frozenset({Boundary.TOOL_CALL})

    async def check(self, db, ec: EnforcementContext) -> GuardrailDecision:
        payload = ec.payload or {}
        allowed_actions = set(payload.get("allowed_actions") or payload.get("allowed_tools") or [])
        action = _tool_name(payload)
        if allowed_actions and action not in allowed_actions:
            return _decision(
                Verdict.BLOCK,
                ViolationKind.UNAUTHORIZED_FLOW,
                f"action {action!r} denied by static policy table",
                {"action": action},
                guardrail=self.name,
            )
        return GuardrailDecision(Verdict.ALLOW, reason="static policy allowed", guardrail=self.name)


def build_system(
    name: str,
    *,
    repo,
    account_id: str,
    loaded: LoadedWorld | None = None,
    acl: ACLService | None = None,
    audit=None,
    runner: Callable[..., Any] | None = None,
) -> System:
    """Build S0/S1/S1p/S2/S2a/S2b without relying on app default assembly."""

    normalized = name.strip()
    if normalized == "S0":
        return System("S0", runner=runner)
    if normalized == "S1":
        return System("S1", guardrails=[GenericGuardrail()], runner=runner)
    if normalized == "S1p":
        return System("S1p", guardrails=[PolicyOnlyGuardrail()], runner=runner)
    if normalized in {"S2", "S2a", "S2b"}:
        loaded_world = loaded or LoadedWorld()
        guardrails = _context_guardrails(normalized, loaded=loaded_world, acl=acl)
        interceptor = EnforcementInterceptor(
            repo,
            account_id,
            loaded_world,
            guardrails=guardrails,
            audit=audit,
        )
        return System(
            normalized,
            guardrails=guardrails,
            interceptor=interceptor,
            runner=runner,
        )
    raise ValueError(f"unknown EntCollabBench system condition: {name}")


def _context_guardrails(
    system_name: str,
    *,
    loaded: LoadedWorld,
    acl: ACLService | None,
) -> list[Any]:
    staleness = StalenessChecker()
    acl_service = acl or ACLService()

    handoff = HandoffGuardrail(
        acl_service,
        staleness,
        object_uri_resolver=loaded.object_uri,
        version_uri_resolver=mapping.resolve_version_tag,
    )
    closure = ClosureGuardrail(staleness)
    tool_state = ToolStateGuardrail(
        staleness,
        role_checker=_role_checker(loaded),
        object_exists=_object_exists(loaded),
        provenance_check=_provenance_check(loaded),
    )

    if system_name == "S2a":
        return [handoff]
    if system_name == "S2b":
        return [closure]
    return [handoff, closure, tool_state]


def _role_checker(loaded: LoadedWorld):
    async def check(agent_id: str, required_role: str) -> bool:
        if agent_id == required_role:
            return True
        return loaded.role_to_owner_space.get(agent_id) == loaded.role_to_owner_space.get(required_role)

    return check


def _object_exists(loaded: LoadedWorld):
    async def check(object_id: str) -> bool:
        return loaded.object_exists(object_id)

    return check


def _provenance_check(loaded: LoadedWorld):
    async def check(arg_name: str, value: str) -> bool:
        if str(value).startswith("ctx://"):
            return str(value).split("@v", 1)[0] in loaded.loaded_uris
        return loaded.object_exists(value)

    return check


async def _evaluate_static_events(
    instance: Mapping[str, Any],
    guardrail: GenericGuardrail | PolicyOnlyGuardrail,
) -> list[dict[str, Any]]:
    events = []
    for event in list(instance.get("events") or instance.get("trace") or []):
        boundary = _boundary(event)
        if boundary not in guardrail.applies_to:
            continue
        ec = EnforcementContext(
            boundary=boundary,
            actor=RequestContext("eval", str(event.get("agent") or event.get("actor") or "agent")),
            recipient=RequestContext("eval", str(event["recipient"]))
            if event.get("recipient")
            else None,
            payload=dict(event.get("payload") or event),
        )
        decision = await guardrail.check(None, ec)
        events.append(
            {
                "boundary": boundary.value,
                "guardrail": decision.guardrail,
                "guardrail_verdict": decision.verdict.value,
                "violations": [violation.kind.value for violation in decision.violations],
                "oracle_violation": event.get("oracle_violation", False),
                "failure_mode": event.get("failure_mode"),
            }
        )
    return events


def _coerce_result(
    value: Any,
    *,
    instance: Mapping[str, Any],
    model: str,
    system: str,
    seed: int,
    subset: str,
) -> InstanceResult:
    if isinstance(value, InstanceResult):
        value.model = value.model or model
        value.system = system
        value.seed = seed
        value.subset = value.subset or subset
        return value

    data = dict(value or {})
    return InstanceResult.from_mapping(
        data,
        instance_id=str(data.get("id") or data.get("instance_id") or instance.get("id") or ""),
        model=model,
        system=system,
        seed=seed,
        subset=str(instance.get("subset") or subset),
    )


def _boundary(event: Mapping[str, Any]) -> Boundary:
    raw = str(event.get("boundary") or event.get("type") or "").lower()
    aliases = {"delegate_start": "handoff", "delegate_done": "handoff"}
    return Boundary(aliases.get(raw, raw))


def _decision(
    verdict: Verdict,
    kind: ViolationKind,
    message: str,
    repair_hint: dict[str, Any] | None = None,
    *,
    guardrail: str = "generic",
) -> GuardrailDecision:
    return GuardrailDecision(
        verdict,
        violations=[Violation(kind, message, repair_hint=repair_hint)],
        reason=message,
        guardrail=guardrail,
    )


def _tool_name(payload: Mapping[str, Any]) -> str:
    contract = payload.get("contract") if isinstance(payload.get("contract"), Mapping) else {}
    return str(
        payload.get("tool_name")
        or payload.get("name")
        or contract.get("tool_name")
        or contract.get("name")
        or ""
    )


def _allowed_tools(payload: Mapping[str, Any]) -> set[str]:
    tools = payload.get("allowed_tools") or payload.get("tool_allowlist") or []
    return {str(tool) for tool in tools}


def _tool_schema(payload: Mapping[str, Any]) -> dict[str, Any]:
    contract = payload.get("contract") if isinstance(payload.get("contract"), Mapping) else {}
    schema = payload.get("inputSchema") or payload.get("tool_schema") or contract.get("arg_schema")
    return dict(schema or {"type": "object", "properties": {}, "required": []})


def _tool_args(payload: Mapping[str, Any]) -> dict[str, Any]:
    args = payload.get("tool_args") or payload.get("arguments") or payload.get("args") or {}
    return dict(args)


def _validate_args(schema: Mapping[str, Any], args: Mapping[str, Any], tool: str) -> list[Violation]:
    violations: list[Violation] = []
    for name in schema.get("required", []):
        if name not in args or _empty(args.get(name)):
            violations.append(
                Violation(
                    ViolationKind.SCHEMA_OR_ENUM,
                    f"missing required arg {name!r} for {tool}",
                    repair_hint={"missing_arg": name},
                )
            )

    properties = schema.get("properties") or {}
    for name, spec in properties.items():
        if name not in args or args[name] is None:
            continue
        if "enum" in spec and args[name] not in spec["enum"]:
            violations.append(
                Violation(
                    ViolationKind.SCHEMA_OR_ENUM,
                    f"arg {name!r}={args[name]!r} not in enum {spec['enum']}",
                    repair_hint={"arg": name, "allowed": spec["enum"], "got": args[name]},
                )
            )
        expected_type = spec.get("type")
        if expected_type and not _type_ok(expected_type, args[name]):
            violations.append(
                Violation(
                    ViolationKind.SCHEMA_OR_ENUM,
                    f"arg {name!r} expected type {expected_type}",
                    repair_hint={"arg": name, "expected_type": expected_type},
                )
            )
    return violations


def _type_ok(expected_type: str, value: Any) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(expected_type, True)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
