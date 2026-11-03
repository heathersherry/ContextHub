"""Offline entity-neighbor/group selection using existing vectors and lexemes.

This is an experimental selector, not a change to ContextHub's default retrieval.
Shared entities are candidate links, not assertions of semantic entailment.
"""

from collections import Counter, defaultdict
from itertools import combinations
import json
import math
import re

import numpy as np

from contexthub.memory.contracts import ref_of


CONFIG = {
    'version': 'connected-query-contribution-v4-no-similarity-dedup',
    'top_k': 15,
    'neighbor_selection': 'raw_query_cosine_greater_than_or_equal_to_threshold',
    'max_group_size': 3,
    'coverage_weight': 0.5,
    'cosine_weight': 0.5,
}

MONTH_STEMS = dict(zip(
    ('januari', 'februari', 'march', 'april', 'may', 'june', 'juli',
     'august', 'septemb', 'octob', 'novemb', 'decemb'), range(1, 13)))
ISO_DATE = re.compile(r'\b\d{4}-(0[1-9]|1[0-2])-\d{2}\b')
QUERY_CUTOFF = re.compile(r'^\s*As of (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?),\s*', re.I)


def split_query_cutoff(query):
    """Separate the observed explicit ISO cutoff prefix from matching content."""
    match = QUERY_CUTOFF.match(query)
    return (match[1], query[match.end():]) if match else (None, query)


def normalize_months(tokens, text):
    result = {f'month:{MONTH_STEMS[t]:02d}' if t in MONTH_STEMS else t for t in tokens}
    result.update(f'month:{m}' for m in ISO_DATE.findall(text))
    return result


def eligible_facts(rows, scope, cutoff):
    """Replay the archived current-fact query; filter state AFTER latest version."""
    latest = {}
    for row in rows:
        if (any(row[key] != value for key, value in scope.items())
                or row['created_in_session'] > cutoff or row['temporal_scope'] != 'current'):
            continue
        previous = latest.get(row['memory_id'])
        if previous is None or row['version'] > previous['version']:
            latest[row['memory_id']] = row
    return {ref_of(row): row for row in latest.values()
            if row['type'] == 'fact' and row['status'] == 'fresh' and row['accepted']
            and not row['invalidated'] and row['memory_embedding'] is not None}


def lexemes(search_vector):
    return {token.replace("''", "'")
            for token in re.findall(r"'((?:[^']|'')*)':", search_vector)}


class EntityGroups:
    def __init__(self, rows, query_vector, query_lexemes, *, neighbor_threshold):
        if not -1 <= neighbor_threshold <= 1:
            raise ValueError('neighbor threshold must be a cosine in [-1, 1]')
        self.neighbor_threshold = neighbor_threshold
        self.rows = rows
        self.refs = sorted(rows)
        self.positions = {ref: i for i, ref in enumerate(self.refs)}
        vectors = [json.loads(rows[ref]['memory_embedding'])
                   if isinstance(rows[ref]['memory_embedding'], str)
                   else rows[ref]['memory_embedding'] for ref in self.refs]
        self.vectors = np.asarray(vectors, dtype=float)
        self.vectors /= np.linalg.norm(self.vectors, axis=1, keepdims=True)
        self.query = np.asarray(query_vector, dtype=float)
        self.query /= np.linalg.norm(self.query)
        self.similarities = self.vectors @ self.query
        self.tokens = {ref: normalize_months(lexemes(rows[ref]['search_vector']), rows[ref]['text'])
                       for ref in self.refs}
        counts = Counter(token for tokens in self.tokens.values() for token in tokens)
        # Unmatchable tokens cannot contribute to the coverage denominator.
        self.weights = {token: math.log((len(rows) + 1) / (counts[token] + 1)) + 1
                        for token in sorted(query_lexemes) if counts[token]}
        self.entities = {ref: set(rows[ref]['entities']) for ref in self.refs}
        self.index = defaultdict(set)
        for ref, entities in self.entities.items():
            for entity in entities:
                self.index[entity].add(ref)
        self.cache = {}

    def coverage(self, refs):
        tokens = set().union(*(self.tokens[ref] for ref in refs))
        return sum(self.weights[t] for t in tokens & self.weights.keys()) / (sum(self.weights.values()) or 1)

    def similarity(self, ref):
        return max(0.0, float(self.similarities[self.positions[ref]]))

    def link(self, left, right):
        shared = self.entities[left] & self.entities[right]
        specificity = max((math.log((len(self.rows) + 1) / (len(self.index[e]) + 1))
                           / math.log(len(self.rows) + 1) for e in shared), default=0.0)
        # An assertion listing many entities is less useful as a bridge.
        quality = specificity / math.sqrt(len(self.entities[left]) * len(self.entities[right])) if shared else 0.0
        return {'left': left, 'right': right, 'entities': sorted(shared), 'quality': quality}

    def score(self, refs):
        refs = tuple(sorted(refs))
        if refs in self.cache:
            return self.cache[refs]
        tokens = set().union(*(self.tokens[ref] for ref in refs))
        matched = sorted(tokens & self.weights.keys())
        coverage = sum(self.weights[t] for t in matched) / (sum(self.weights.values()) or 1)
        vector = self.vectors[[self.positions[ref] for ref in refs]].sum(axis=0)
        norm = np.linalg.norm(vector)
        cosine = float(vector @ self.query / norm) if norm else 0.0
        links = sorted((self.link(a, b) for a, b in combinations(refs, 2)),
                       key=lambda edge: edge['quality'], reverse=True)
        # At most three members: the best n-1 edges form a maximum spanning tree.
        tree = links[:len(refs) - 1]
        connected = all(edge['entities'] for edge in tree)
        result = {'refs': list(refs), 'coverage': coverage, 'matched_lexemes': matched,
                  'cosine': cosine, 'links': tree, 'connected': connected,
                  'weakest_link': min((e['quality'] for e in tree), default=1.0)}
        self.cache[refs] = result
        return result

    def groups(self, seeds, expand=True):
        seeds = sorted(set(seeds))
        groups = {(ref,) for ref in seeds}
        if not expand:
            return [self.score(group) for group in sorted(groups)], {}
        adjacency = defaultdict(set)
        neighborhoods = {}
        for seed in seeds:
            neighbors = set().union(*(self.index[e] for e in self.entities[seed])) - {seed}
            def neighbor_value(ref):
                return float(self.similarities[self.positions[ref]])
            ranked = sorted(neighbors, key=lambda ref: (-neighbor_value(ref), ref))
            kept = [ref for ref in ranked if neighbor_value(ref) >= self.neighbor_threshold]
            neighborhoods[seed] = {'available': len(neighbors), 'kept': kept,
                                   'scores': {ref: neighbor_value(ref) for ref in kept}}
            for neighbor in kept:
                adjacency[seed].add(neighbor)
                adjacency[neighbor].add(seed)
                groups.add(tuple(sorted((seed, neighbor))))
        for center, neighbors in sorted(adjacency.items()):
            for left, right in combinations(sorted(neighbors), 2):
                groups.add(tuple(sorted((left, center, right))))
        return [self.score(group) for group in sorted(groups)], neighborhoods

    def bridge_paths(self, groups, seeds):
        """Credit a middle fact only for joining complementary, relevant seeds.

        Endpoint relevance depends on the query. A low-similarity middle fact can
        still matter, but a shared entity alone earns no bridge credit. These are
        co-occurrence paths; no semantic entailment is inferred.
        """
        paths = []
        seeds = set(seeds)
        for group in groups:
            if len(group['refs']) != 3:
                continue
            for center in group['refs']:
                left, right = [ref for ref in group['refs'] if ref != center]
                if left not in seeds or right not in seeds or self.entities[left] & self.entities[right]:
                    continue
                edges = [self.link(left, center), self.link(center, right)]
                if not all(edge['entities'] for edge in edges):
                    continue
                complementary = self.coverage((left, right)) - max(self.coverage((left,)), self.coverage((right,)))
                strength = min(e['quality'] for e in edges) * min(self.similarity(left), self.similarity(right)) * complementary
                if strength > 0:
                    paths.append({'refs': group['refs'], 'center': center, 'endpoints': [left, right],
                                  'complementary_coverage': complementary, 'links': edges, 'strength': strength})
        return paths

    def contribution(self, group, selected, groups, paths, best_coverage, best_path):
        new = [ref for ref in group['refs'] if ref not in selected]
        expanded = selected | set(new)
        # A date from one trip and a booking from another cannot jointly satisfy
        # the query merely because both facts occupy the evidence budget.
        complete_groups = [g for g in groups if set(g['refs']) <= expanded]
        best_group = max(complete_groups, key=lambda g: g['coverage'])
        coverage_gain = max(0.0, best_group['coverage'] - best_coverage)
        direct = [{'ref': ref, 'query_cosine': self.similarity(ref)} for ref in new]
        # Only newly completed paths can improve the best available connection.
        enabled = [p for p in paths if set(p['refs']) <= expanded]
        strongest = max(enabled, key=lambda p: p['strength'], default=None)
        path_value = strongest['strength'] if strongest else 0.0
        bridge_gain = max(0.0, path_value - best_path)
        value = (CONFIG['coverage_weight'] * coverage_gain
                 + CONFIG['cosine_weight'] * sum(d['query_cosine'] for d in direct)
                 + bridge_gain) / len(new)
        return {'added': new, 'coverage_gain': coverage_gain, 'direct_relevance': direct,
                'bridge_gain': bridge_gain, 'best_path': strongest, 'best_coverage_group': best_group,
                'selection_score': value}

    def select(self, seeds, expand=True, budget=15):
        groups, neighborhoods = self.groups(seeds, expand)
        paths = self.bridge_paths(groups, seeds)
        selected, trace, best_path, best_coverage = [], [], 0.0, 0.0
        while len(selected) < budget:
            best = None
            for group in groups:
                new = [ref for ref in group['refs'] if ref not in selected]
                if not new or len(selected) + len(new) > budget:
                    continue
                contribution = self.contribution(group, set(selected), groups, paths, best_coverage, best_path)
                key = (contribution['selection_score'], -len(new), tuple(group['refs']))
                if best is None or key > best[0]:
                    best = (key, group, contribution)
            if best is None:
                break
            _, group, contribution = best
            selected.extend(contribution['added'])
            if contribution['best_path']:
                best_path = max(best_path, contribution['best_path']['strength'])
            best_coverage = max(best_coverage, contribution['best_coverage_group']['coverage'])
            trace.append({**group, **contribution, 'used_slots': len(selected)})
        return {'selected_refs': selected, 'trace': trace, 'neighborhoods': neighborhoods,
                'candidate_refs': sorted({ref for group in groups for ref in group['refs']}),
                'group_count': len(groups), 'bridge_paths': paths}
