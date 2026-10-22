"""Grade Tracker - Academic grade tracking and reporting toolkit."""

from grade_tracker.policies import (
    DEFAULT_POLICY,
    Policy,
    PolicyRegistry,
    UnknownPolicyError,
    default_policy,
    get_default_registry,
    list_policies,
    register_policy,
    resolve_policy,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_POLICY",
    "Policy",
    "PolicyRegistry",
    "UnknownPolicyError",
    "default_policy",
    "get_default_registry",
    "list_policies",
    "register_policy",
    "resolve_policy",
]
