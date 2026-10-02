"""Four conservative entity sieves. No defaults, NER verdicts or ID merging."""

import re
from typing import Literal
from pydantic import Field, model_validator
from contexthub.discovery.mention_ner import SpacyMentionConfig
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    EvidenceRef,
    Name,
    Nonnegative,
    text_hash,
)


class Mention(Contract):
    text: Name
    evidence: EvidenceRef
    # Scope is a source-backed literal qualifier, never a guessed entity label.
    scope: Name | None = None
    start: Nonnegative | None = None

    @model_validator(mode="after")
    def located(self):
        if self.text not in self.evidence.excerpt:
            raise ContractError("mention_not_in_evidence")
        if self.scope and self.scope not in self.evidence.excerpt:
            raise ContractError("scope_not_in_evidence")
        positions = [
            m.start() for m in re.finditer(re.escape(self.text), self.evidence.excerpt)
        ]
        if self.start is None and len(positions) == 1:
            object.__setattr__(self, "start", self.evidence.start + positions[0])
        if self.start is not None:
            offset = self.start - self.evidence.start
            if (
                offset < 0
                or self.evidence.excerpt[offset : offset + len(self.text)] != self.text
            ):
                raise ContractError("mention_position_mismatch")
        return self


class PatternRule(Contract):
    id: Name
    version: Name
    kind: Literal["explicit_alias", "incompatible_types", "different_identity"]
    # Full original statement, named left/right groups; no NER tag input.
    pattern: Name

    @model_validator(mode="after")
    def compiled(self):
        if not {"left", "right"} <= re.compile(self.pattern).groupindex.keys():
            raise ContractError("rule_requires_scoped_pair")
        return self


class Alias(Contract):
    name: Name
    scope: Name
    canonical_id: Name


class Stage0Config(Contract):
    version: Name
    rules: tuple[PatternRule, ...]
    alias_version: Name
    aliases: tuple[Alias, ...]
    # Candidate mention locator only. It cannot give a verdict.
    mention_patterns: tuple[Name, ...]
    mention_view: Literal["raw-v1", "dialogue-content-v2"] = Field(
        default="raw-v1", exclude_if=lambda value: value == "raw-v1"
    )

    spacy_ner: SpacyMentionConfig | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def valid(self):
        if self.spacy_ner is not None and (self.mention_view != "dialogue-content-v2" or self.mention_patterns):
            raise ContractError("spacy_mentions_require_dialogue_without_regex")
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ContractError("duplicate_rule")
        for p in self.mention_patterns:
            if "mention" not in re.compile(p).groupindex:
                raise ContractError("mention_pattern_requires_group")
        return self


class RuleHit(Contract):
    rule_id: Name
    rule_version: Name
    verdict: Literal["SAME", "DIFFERENT"]
    evidence: tuple[EvidenceRef, ...]
    alias_version: Name | None = None
    canonical_id: Name | None = None


class Stage0Result(Contract):
    verdict: Literal["SAME", "DIFFERENT", "UNRESOLVED"]
    hits: tuple[RuleHit, ...]
    reason: Name
    provenance_retained: bool = True


def locate(config, evidence):
    if config.mention_view == "dialogue-content-v2":
        return _locate_dialogue(config, evidence)
    mentions = []
    for e in evidence:
        for pattern in config.mention_patterns:
            for m in re.finditer(pattern, e.excerpt):
                scope = m.groupdict().get("scope")
                snippet = e.excerpt[m.start() : m.end()]
                located = e.model_copy(
                    update={
                        "start": e.start + m.start(),
                        "end": e.start + m.end(),
                        "excerpt": snippet,
                        "excerpt_sha256": text_hash(snippet),
                    }
                )
                item = Mention(
                    text=m.group("mention"),
                    evidence=located,
                    scope=scope,
                    start=e.start + m.start("mention"),
                )
                if item not in mentions:
                    mentions.append(item)
    return tuple(mentions)



def _locate_dialogue(config, evidence):
    from contexthub.discovery.mention_view import dialogue_views

    if config.spacy_ner is not None:
        from contexthub.discovery.mention_ner import check_ner
        check_ner(config.spacy_ner)
    mentions, seen = [], set()
    sources = {}
    for ref in evidence:
        sources.setdefault(ref.source, []).append(ref)
    for refs in sources.values():
        # A full source is provided by read_evidence. Overlapping extraction
        # fragments must not become separate candidate occurrences.
        full = max(refs, key=lambda ref: ref.end - ref.start)
        if full.start != 0 or any(ref.end > full.end for ref in refs):
            raise ContractError("complete_dialogue_evidence_required")
        for ref in refs:
            if full.excerpt[ref.start:ref.end] != ref.excerpt:
                raise ContractError("overlapping_mention_evidence_mismatch")
        for text, mapping in dialogue_views(full.excerpt):
            for a, b, scope_span in _candidate_spans(config, text):
                if not 0 <= a < b <= len(mapping):
                    raise ContractError("invalid_candidate_mention_span")
                start, end = mapping[a][0], mapping[b - 1][1]
                snippet = full.excerpt[start:end]
                key = (full.source, start, end)
                if key in seen:
                    continue
                seen.add(key)
                provenance = tuple(dict.fromkeys(p for ref in refs for p in ref.provenance))
                backing = full.model_copy(update={"provenance": provenance})
                scope = None
                if scope_span:
                    sa, sb = scope_span
                    scope = full.excerpt[mapping[sa][0]:mapping[sb - 1][1]]
                mentions.append(Mention(text=snippet, evidence=backing, start=start, scope=scope))
    return tuple(mentions)


def _candidate_spans(config, text):
    if config.spacy_ner is not None:
        from contexthub.discovery.mention_ner import entity_spans
        return ((a, b, None) for a, b in entity_spans(config.spacy_ner, text))
    return ((match.start("mention"), match.end("mention"),
             match.span("scope") if match.groupdict().get("scope") else None)
            for pattern in config.mention_patterns for match in re.finditer(pattern, text))


def apply_stage0(
    config: Stage0Config,
    left: Mention,
    right: Mention,
    evidence: tuple[EvidenceRef, ...],
) -> Stage0Result:
    hits = []
    ambiguous = left.start is None or right.start is None
    # A matched relation must reference these exact occurrences in this source.
    for rule in config.rules:
        for e in evidence:
            for match in re.finditer(rule.pattern, e.excerpt):
                if (match.group("left"), match.group("right")) not in (
                    (left.text, right.text),
                    (right.text, left.text),
                ):
                    continue
                if (
                    left.evidence.source != e.source
                    or right.evidence.source != e.source
                ):
                    ambiguous = True
                    continue
                if not all(
                    e.start <= m.evidence.start and m.evidence.end <= e.end
                    for m in (left, right)
                ):
                    ambiguous = True
                    continue
                groups = [
                    (match.group(name), e.start + match.start(name))
                    for name in ("left", "right")
                ]
                if (left.text, left.start) not in groups or (
                    right.text,
                    right.start,
                ) not in groups:
                    continue
                excerpt = match.group(0)
                located = e.model_copy(
                    update={
                        "start": e.start + match.start(),
                        "end": e.start + match.end(),
                        "excerpt": excerpt,
                        "excerpt_sha256": text_hash(excerpt),
                    }
                )
                hits.append(
                    RuleHit(
                        rule_id=rule.id,
                        rule_version=rule.version,
                        verdict="SAME"
                        if rule.kind == "explicit_alias"
                        else "DIFFERENT",
                        evidence=(located,),
                    )
                )
    ids = []
    for mention in (left, right):
        found = {
            a.canonical_id
            for a in config.aliases
            if a.name == mention.text and a.scope == mention.scope
        }
        if len(found) > 1:
            ambiguous = True
        ids.append(found)
    if len(ids[0]) == len(ids[1]) == 1 and ids[0] == ids[1]:
        hits.append(
            RuleHit(
                rule_id="versioned_alias_table",
                rule_version=config.version,
                verdict="SAME",
                evidence=(left.evidence, right.evidence),
                alias_version=config.alias_version,
                canonical_id=next(iter(ids[0])),
            )
        )
    verdicts = {h.verdict for h in hits}
    resolved = not ambiguous and len(verdicts) == 1
    return Stage0Result(
        verdict=next(iter(verdicts)) if resolved else "UNRESOLVED",
        hits=tuple(hits),
        reason="rule_match" if resolved else "ambiguous_conflict_or_no_rule",
    )
