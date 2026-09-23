"""Explicit core extraction, search and S4 dependency discovery interfaces."""

from .extraction import ExtractionConfig, ExtractionService
from .search import CandidatePage, HistorySnapshot, SearchPolicy, SearchService

__all__ = [
    "ExtractionConfig",
    "ExtractionService",
    "CandidatePage",
    "HistorySnapshot",
    "SearchPolicy",
    "SearchService",
]

from .stage0 import Stage0Config, Stage0Result, Mention, apply_stage0
from .adjudication import AdjudicationConfig, AdjudicationService, EvidenceSet, Judgment
from .planner import Plan, Item, choose_price, solve_mckp
from .service import DiscoveryConfig, DiscoveryService
from .contracts import DiscoveryReport

__all__ += ["Stage0Config", "Stage0Result", "Mention", "apply_stage0",
            "AdjudicationConfig", "AdjudicationService", "EvidenceSet", "Judgment",
            "Plan", "Item", "choose_price", "solve_mckp", "DiscoveryConfig",
            "DiscoveryService", "DiscoveryReport"]
