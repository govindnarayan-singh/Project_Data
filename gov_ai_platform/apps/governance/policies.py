from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from .exceptions import GovernanceApprovalError


@dataclass(frozen=True)
class SubjectSnapshot:
    version: int
    organization_id: int
    digest: str = ""
    state: str = ""


@dataclass(frozen=True)
class ApprovalPolicy:
    workflow_type: str
    required_reviewer_role: str
    prohibit_self_approval: bool
    read_subject: Callable[[str], SubjectSnapshot]
    can_review_organization: Callable[[object, int], bool]
    allowed_decisions: frozenset[str]


_POLICIES: dict[str, ApprovalPolicy] = {}


def get_policy(workflow_type: str) -> ApprovalPolicy:
    policy = _POLICIES.get(workflow_type)
    if policy is None:
        raise GovernanceApprovalError("UNKNOWN_WORKFLOW", "Unknown approval workflow type; access denied.")
    return policy


def register_policy(policy: ApprovalPolicy) -> None:
    if not policy.workflow_type or policy.workflow_type in _POLICIES:
        raise GovernanceApprovalError("POLICY_REGISTRATION", "Approval policy is missing or already registered.")
    _POLICIES[policy.workflow_type] = policy


@contextmanager
def temporary_policy(policy: ApprovalPolicy) -> Iterator[None]:
    register_policy(policy)
    try:
        yield
    finally:
        _POLICIES.pop(policy.workflow_type, None)
