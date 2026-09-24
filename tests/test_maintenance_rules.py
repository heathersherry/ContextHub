"""Deterministic rule fixtures used by the real S6 core pipeline."""

from contexthub.propagation.contracts import RequiredInput
from contexthub.propagation.candidate import GenerationResponse, Content
from s6_helpers import make_data


class SyntheticRule:
    id = "synthetic-rule"
    version = "v1"

    def __init__(self, source, *, hit=True, produce=True):
        self.source, self.hit, self.produce = source, hit, produce

    def required_inputs(self, target):
        return (
            RequiredInput(
                source=self.source,
                role="rule",
                rule_id=self.id,
                rule_version=self.version,
            ),
        )

    def applies(self, target, original):
        return self.hit

    def execute(self, original, inputs, causes):
        return GenerationResponse(
            proposal=make_data(
                Content(
                    l0_content=None,
                    l1_content=None,
                    l2_content="synthetic rule conclusion",
                ),
                inputs,
                causes,
            )
            if self.produce
            else None,
            unresolved_reason=None if self.produce else "rule_not_resolved",
        )


def test_synthetic_rule_same_input_same_output():
    from s6_helpers import sample_data

    data = sample_data()
    rule = SyntheticRule(data.causes[0].cause.source)
    args = (data.content, data.claims[0].evidence, (data.causes[0].cause,))
    assert rule.execute(*args) == rule.execute(*args)
