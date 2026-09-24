"""Explicit S5 core entry points; importing does not enable event routing."""

from contexthub.propagation.contracts import (
    MaintenancePolicy,
    InputRegistration,
    RequiredInput,
)
from contexthub.propagation.executor import MaintenanceExecutor
from contexthub.propagation.evidence import EvidenceBuilder
from contexthub.propagation.candidate import CandidateGenerator, ModelConfig, DeterministicRule
from contexthub.propagation.hard_checks import HardChecks
from contexthub.propagation.verifier import FixedVerifier
from contexthub.propagation.profiles import MaintenanceRunner, ProfileConfig
from contexthub.propagation.commit import GuardedCommit

__all__ = [
    "MaintenancePolicy",
    "InputRegistration",
    "RequiredInput",
    "MaintenanceExecutor",
    "EvidenceBuilder",
    "CandidateGenerator", "ModelConfig", "DeterministicRule", "HardChecks",
    "FixedVerifier", "MaintenanceRunner", "ProfileConfig", "GuardedCommit",
]
