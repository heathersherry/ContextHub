"""Cheap, auditable identity candidates for a specific directed fact pair.

Routing is not a SAME/DIFFERENT judgment and never filters dependency pairs.
"""
import json
import re
import unicodedata
from typing import Literal
from uuid import UUID
from pydantic import Field
from contexthub.models.knowledge import Contract, Name, Nonnegative, canonical_hash
from contexthub.discovery.stage0 import Mention


class IdentityRoutingConfig(Contract):
    version: Literal['fact-pair-names-v1'] = 'fact-pair-names-v1'
    unknown_policy: Literal['block', 'advisory'] = Field(default='block', exclude_if=lambda v: v == 'block')
    matching: tuple[Literal['exact', 'name_token_overlap', 'initialism', 'explicit_alias'], ...]


class IdentityRoute(Contract):
    upstream_id: UUID
    downstream_id: UUID
    generation: Nonnegative
    left_mentions: Nonnegative
    right_mentions: Nonnegative
    possible_position_pairs: Nonnegative
    selected_pairs: Nonnegative
    reason: Name
    resolution_keys: tuple[Name, ...] = ()
    processed_resolution_keys: tuple[Name, ...] = ()
    reused_resolutions: Nonnegative = 0
    matching_reasons: tuple[Name, ...] = ()


def normalized(text):
    # Only for candidate routing. Never rewrite source-backed Mention text/spans.
    try:
        text = json.loads('"' + text + '"')
    except ValueError:
        pass
    return unicodedata.normalize('NFKC', text).casefold()


def tokens(text):
    return tuple(re.findall(r'[^\W_]+', normalized(text)))


def occurrence_key(mention):
    return canonical_hash(mention)


def attached_mentions(candidate, mentions):
    """Require both a name in fact text and its occurrence in cited input spans."""
    text = normalized(candidate.text)
    refs = candidate.evidence
    result = []
    for mention in mentions:
        if mention.start is None:
            continue
        name = normalized(mention.text)
        # English token boundary prevents Ann attaching to annual. Chinese names
        # need literal substring matching because Chinese words need no spaces.
        present = (name in text if re.search(r'[\u3400-\u9fff]', name)
                   else bool(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text)))
        if not present:
            continue
        allowed = any(ref.source == mention.evidence.source
                      and ref.start <= mention.start
                      and mention.start + len(mention.text) <= ref.end for ref in refs)
        if candidate.kind == 'history_source':
            allowed = mention.evidence.source in candidate.sources
        if allowed:
            result.append(mention)
    return tuple(result)


def match_reason(left, right, config):
    if occurrence_key(left) == occurrence_key(right):
        return None  # The very same source occurrence requires no identity call.
    a, b = tokens(left.text), tokens(right.text)
    if not a or not b:
        return None
    if 'exact' in config.matching and a == b:
        return 'same_spelling_not_same_identity'
    if 'name_token_overlap' in config.matching and set(a) & set(b):
        return 'shared_name_token'
    if 'initialism' in config.matching:
        for short, long in ((a, b), (b, a)):
            abbreviation = ''.join(short)
            if len(long) >= 2 and len(abbreviation) >= 2 and abbreviation == ''.join(w[0] for w in long):
                return 'possible_initialism'
    if ('explicit_alias' in config.matching and left.evidence.source == right.evidence.source
            and left.start is not None and right.start is not None):
        first, last = sorted((left, right), key=lambda m: m.start)
        begin, end = first.start + len(first.text), last.start
        ref = first.evidence
        if ref.start <= begin <= end <= ref.end:
            between = ref.excerpt[begin-ref.start:end-ref.start]
            if len(between) <= 80 and re.fullmatch(r'''[\s\"'(),，:：（）]*(?:aka|(?:is )?also known as|(?:is )?called|简称|又名|也叫)[\s\"'(),，:：（）]*''', normalized(between)):
                return 'source_alias_phrase'
    return None


def route_pair(candidate, target, mentions, config, generation):
    left, right = attached_mentions(candidate, mentions), attached_mentions(target, mentions)
    pairs, seen = [], set()
    for a in left:
        for b in right:
            why = match_reason(a, b, config)
            if why is None:
                continue
            key = canonical_hash(sorted((occurrence_key(a), occurrence_key(b))))
            if key not in seen:
                seen.add(key)
                pairs.append((key, a, b, why))
    audit = IdentityRoute(upstream_id=candidate.id, downstream_id=target.id,
        generation=generation, left_mentions=len(left), right_mentions=len(right),
        possible_position_pairs=len(left)*len(right), selected_pairs=len(pairs),
        reason='possible_coreference_for_fact_pair' if pairs else 'no_identity_pair_routed_dependency_still_judged',
        resolution_keys=tuple(p[0] for p in pairs), matching_reasons=tuple(p[3] for p in pairs))
    return pairs, audit
