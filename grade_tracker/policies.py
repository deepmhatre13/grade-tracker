"""Versioned, configurable grading policies for Grade Tracker.

A *policy* captures every rule the calculation engine needs in order to turn a
numeric score into a letter grade and a grade point:

* ``identifier`` / ``version`` -- a stable, human-readable identity.  A
  version is immutable once registered, so historical results stay
  reproducible.
* ``thresholds`` -- inclusive minimum percentages, e.g. ``((90, "A"), (80,
  "B"))``.  A score exactly equal to a threshold receives that threshold's
  letter grade.
* ``grade_points`` -- the grade points awarded for each letter grade.
* ``gpa_rounding`` / ``gpa_rounding_mode`` -- how (and whether) the final GPA
  is rounded.
* ``percentage_rounding`` -- optional rounding of *reported* percentages.
  Classification always uses the unrounded percentage.
* ``failing_letter`` -- the letter grade awarded below the lowest threshold
  (historically ``"F"``).
* ``additional_rules`` -- optional callables that can override the
  threshold-based classification.

The default policy, ``legacy`` version ``1``, reproduces the behaviour that
Grade Tracker had before policies existed: thresholds 90/80/70/60, grade
points A=10, B=9, C=8, D=7, F=0, and a 2-decimal GPA.  It is the default for
every public calculation API, so existing datasets keep their results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Callable, Dict, Iterable, List, Optional, Tuple


__all__ = [
    "Policy",
    "PolicyRegistry",
    "PolicyError",
    "DuplicatePolicyError",
    "UnknownPolicyError",
    "DEFAULT_POLICY_IDENTIFIER",
    "DEFAULT_POLICY_VERSION",
    "DEFAULT_POLICY",
    "PolicyRule",
    "default_policy",
    "get_default_registry",
    "resolve_policy",
    "list_policies",
    "register_policy",
]


DEFAULT_POLICY_IDENTIFIER = "legacy"
DEFAULT_POLICY_VERSION = "1"

# A custom rule receives the unrounded percentage and the policy that is
# being applied, and returns a letter grade to use instead of the
# threshold-based one (or ``None`` to fall back to the thresholds).
PolicyRule = Callable[[Decimal, "Policy"], Optional[str]]


class PolicyError(Exception):
    """Base class for all grading-policy errors."""


class DuplicatePolicyError(PolicyError):
    """Raised when a policy identity is registered more than once."""


class UnknownPolicyError(PolicyError):
    """Raised when a requested policy is not registered."""


@dataclass(frozen=True)
class Policy:
    """An immutable, versioned grading policy.

    Attributes:
        identifier: Stable name of the policy, e.g. ``"legacy"``.
        version: Version string of the policy, e.g. ``"1"``.
        thresholds: Inclusive minimum percentages paired with a letter grade,
            ordered from highest to lowest.
        grade_points: Grade points awarded for each letter grade.
        gpa_rounding: Number of decimals the final GPA is rounded to, or
            ``None`` to skip rounding.
        gpa_rounding_mode: ``"half_up"``, ``"half_even"``, or ``"python"``.
            ``"python"`` reproduces the historical built-in :func:`round`
            behaviour exactly and is used by the default policy.
        percentage_rounding: Number of decimals used when *reporting* a
            percentage, or ``None`` for full precision.  Classification always
            uses the unrounded percentage.
        failing_letter: Letter grade awarded below the lowest threshold.
        additional_rules: Optional custom rules evaluated before the
            thresholds.
        description: Optional human-readable description.
    """

    identifier: str
    version: str
    thresholds: Tuple[Tuple[float, str], ...]
    grade_points: Dict[str, float]
    gpa_rounding: Optional[int] = 2
    gpa_rounding_mode: str = "half_up"
    percentage_rounding: Optional[int] = None
    failing_letter: str = "F"
    additional_rules: Tuple[PolicyRule, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not str(self.identifier).strip():
            raise ValueError("policy identifier must not be empty")
        if not str(self.version).strip():
            raise ValueError("policy version must not be empty")
        if not self.thresholds:
            raise ValueError("policy must define at least one threshold")

        normalised: List[Tuple[float, str]] = []
        seen_letters = set()
        previous = None
        for index, (percentage, letter) in enumerate(self.thresholds, start=1):
            value = float(percentage)
            if not math.isfinite(value) or value < 0 or value > 100:
                raise ValueError(
                    "threshold percentage must be between 0 and 100, got "
                    f"{percentage!r}"
                )
            letter = str(letter)
            if not letter:
                raise ValueError(f"threshold {index} has an empty letter grade")
            if letter in seen_letters:
                raise ValueError(
                    f"duplicate letter grade {letter!r} in thresholds"
                )
            seen_letters.add(letter)
            if previous is not None and value >= previous:
                raise ValueError(
                    "thresholds must be strictly descending by percentage "
                    f"(got {previous} before {value})"
                )
            previous = value
            normalised.append((value, letter))

        object.__setattr__(self, "thresholds", tuple(normalised))

        if not isinstance(self.grade_points, dict):
            raise TypeError("grade_points must be a dictionary")
        points = {str(k): float(v) for k, v in self.grade_points.items()}
        for letter, value in points.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"grade points for {letter!r} must be a finite, "
                    f"non-negative number, got {value!r}"
                )
        for _, letter in normalised:
            if letter not in points:
                raise ValueError(
                    f"grade points are missing for letter grade {letter!r}"
                )
        if self.failing_letter not in points:
            raise ValueError(
                "grade points are missing for the failing letter grade "
                f"{self.failing_letter!r}"
            )
        object.__setattr__(self, "grade_points", points)
        if self.gpa_rounding is not None:
            if isinstance(self.gpa_rounding, bool) or not isinstance(
                self.gpa_rounding, int
            ):
                raise ValueError("gpa_rounding must be an integer or None")
            if self.gpa_rounding < 0:
                raise ValueError("gpa_rounding must not be negative")
        if self.gpa_rounding_mode not in ("half_up", "half_even", "python"):
            raise ValueError(
                "gpa_rounding_mode must be 'half_up', 'half_even' or "
                f"'python', got {self.gpa_rounding_mode!r}"
            )
        if self.percentage_rounding is not None:
            if isinstance(self.percentage_rounding, bool) or not isinstance(
                self.percentage_rounding, int
            ):
                raise ValueError(
                    "percentage_rounding must be an integer or None"
                )
            if self.percentage_rounding < 0:
                raise ValueError("percentage_rounding must not be negative")

        object.__setattr__(
            self, "additional_rules", tuple(self.additional_rules or ())
        )

    # -- identity ---------------------------------------------------------

    @property
    def key(self) -> Tuple[str, str]:
        """The registry key of this policy: ``(identifier, version)``."""
        return (self.identifier, self.version)

    @property
    def name(self) -> str:
        """The canonical ``identifier@version`` name of this policy."""
        return f"{self.identifier}@{self.version}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name

    # -- classification ---------------------------------------------------

    def grade_points_for(self, letter: str) -> float:
        """Return the grade points for ``letter`` (0.0 when unknown)."""
        return float(self.grade_points.get(letter, 0.0))

    def letter_for_percentage(self, pct: Decimal) -> str:
        """Classify an unrounded percentage into a letter grade.

        Thresholds are inclusive: a percentage exactly equal to a threshold
        receives that threshold's letter grade.  Custom rules are evaluated
        first and win when they return a letter grade.
        """
        for rule in self.additional_rules:
            letter = rule(pct, self)
            if letter:
                return str(letter)

        for threshold, letter in self.thresholds:
            if pct >= Decimal(str(threshold)):
                return letter
        return self.failing_letter

    # -- rounding ---------------------------------------------------------

    def round_gpa(self, value: float) -> float:
        """Round a GPA using the policy's rounding rule.

        ``"python"`` mode reproduces the historical built-in :func:`round`
        behaviour (round-half-to-even on the binary float), which is what
        Grade Tracker used before policies existed.
        """
        if self.gpa_rounding is None:
            return float(value)
        if self.gpa_rounding_mode == "python":
            return round(float(value), self.gpa_rounding)
        quantum = Decimal(1).scaleb(-self.gpa_rounding)
        rounding = (
            ROUND_HALF_UP
            if self.gpa_rounding_mode == "half_up"
            else ROUND_HALF_EVEN
        )
        return float(Decimal(str(value)).quantize(quantum, rounding=rounding))

    def round_percentage(self, pct: Decimal) -> Decimal:
        """Round a percentage for reporting.

        Classification never uses this value; it exists so a policy can pin
        the precision shown to users.
        """
        if self.percentage_rounding is None:
            return pct
        quantum = Decimal(1).scaleb(-self.percentage_rounding)
        return pct.quantize(quantum, rounding=ROUND_HALF_UP)

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-friendly description of this policy."""
        return {
            "identifier": self.identifier,
            "version": self.version,
            "name": self.name,
            "thresholds": [
                {"percentage": percentage, "letter": letter}
                for percentage, letter in self.thresholds
            ],
            "grade_points": dict(self.grade_points),
            "gpa_rounding": self.gpa_rounding,
            "gpa_rounding_mode": self.gpa_rounding_mode,
            "percentage_rounding": self.percentage_rounding,
            "failing_letter": self.failing_letter,
            "description": self.description,
        }


class PolicyRegistry:
    """A registry of versioned grading policies.

    Policies are keyed by the ``(identifier, version)`` pair, so a published
    version can never be silently redefined.  Registering the same key twice
    raises :class:`DuplicatePolicyError` unless ``replace=True`` is passed
    explicitly.
    """

    def __init__(self, policies: Optional[Iterable[Policy]] = None) -> None:
        self._policies: Dict[Tuple[str, str], Policy] = {}
        for policy in policies or ():
            self.register(policy)

    # -- registration -----------------------------------------------------

    def register(self, policy: Policy, replace: bool = False) -> Policy:
        """Register ``policy`` and return it.

        Raises:
            DuplicatePolicyError: if the policy identity is already
                registered and ``replace`` is False.
        """
        if not isinstance(policy, Policy):
            raise TypeError("register() expects a Policy instance")
        if policy.key in self._policies and not replace:
            raise DuplicatePolicyError(
                f"policy {policy.name!r} is already registered; "
                "register a new version instead of redefining an existing one"
            )
        self._policies[policy.key] = policy
        return policy

    def unregister(self, identifier: str, version: str) -> None:
        """Remove a policy from the registry (used mainly by tests)."""
        self._policies.pop((identifier, version), None)

    # -- lookup -----------------------------------------------------------

    def __contains__(self, key: object) -> bool:
        if isinstance(key, Policy):
            return key.key in self._policies
        if isinstance(key, tuple):
            return key in self._policies
        if isinstance(key, str):
            try:
                identifier, version = self._split_name(key)
            except ValueError:
                return False
            return (identifier, version) in self._policies
        return False

    def __len__(self) -> int:
        return len(self._policies)

    @staticmethod
    def _split_name(name: str) -> Tuple[str, str]:
        """Split ``"identifier@version"`` into its two parts."""
        if "@" not in name:
            raise ValueError(
                f"invalid policy name {name!r}; expected 'identifier@version'"
            )
        identifier, _, version = name.partition("@")
        if not identifier or not version:
            raise ValueError(
                f"invalid policy name {name!r}; expected 'identifier@version'"
            )
        return identifier, version

    def get(self, identifier: str, version: Optional[str] = None) -> Policy:
        """Return a registered policy.

        ``version`` may be omitted to fetch the highest registered version of
        ``identifier``.

        Raises:
            UnknownPolicyError: if no such policy is registered.
        """
        if version is None:
            versions = sorted(
                key[1] for key in self._policies if key[0] == identifier
            )
            if not versions:
                raise UnknownPolicyError(
                    f"unknown grading policy {identifier!r}; "
                    f"available policies: {', '.join(self.names())}"
                )
            version = versions[-1]
        policy = self._policies.get((identifier, version))
        if policy is None:
            raise UnknownPolicyError(
                f"unknown grading policy {identifier!r} version {version!r}; "
                f"available policies: {', '.join(self.names())}"
            )
        return policy

    def resolve(self, name: str) -> Policy:
        """Resolve a policy from a name such as ``"legacy"`` or ``"legacy@1"``."""
        if not isinstance(name, str) or not name.strip():
            raise UnknownPolicyError("a grading policy name is required")
        name = name.strip()
        if "@" in name:
            identifier, version = self._split_name(name)
            return self.get(identifier, version)
        return self.get(name)

    def names(self) -> List[str]:
        """Return the sorted ``identifier@version`` names of all policies."""
        return sorted(f"{key[0]}@{key[1]}" for key in self._policies)

    def all(self) -> List[Policy]:
        """Return every registered policy, ordered by name."""
        return [self._policies[key] for key in sorted(self._policies)]


# ---------------------------------------------------------------------------
# Default policy and registry
# ---------------------------------------------------------------------------

#: The policy that reproduces Grade Tracker's historical behaviour.
DEFAULT_POLICY = Policy(
    identifier=DEFAULT_POLICY_IDENTIFIER,
    version=DEFAULT_POLICY_VERSION,
    thresholds=((90, "A"), (80, "B"), (70, "C"), (60, "D")),
    grade_points={"A": 10.0, "B": 9.0, "C": 8.0, "D": 7.0, "F": 0.0},
    gpa_rounding=2,
    gpa_rounding_mode="python",
    percentage_rounding=None,
    failing_letter="F",
    description=(
        "Historical Grade Tracker policy: A>=90, B>=80, C>=70, D>=60, "
        "F below 60; grade points A=10, B=9, C=8, D=7, F=0."
    ),
)

_DEFAULT_REGISTRY = PolicyRegistry([DEFAULT_POLICY])


def get_default_registry() -> PolicyRegistry:
    """Return the process-wide default policy registry.

    The registry is shared, so third-party code can register additional
    policies and have them discoverable by the CLI.
    """
    return _DEFAULT_REGISTRY


def default_policy() -> Policy:
    """Return the default (``legacy@1``) policy."""
    return DEFAULT_POLICY


def register_policy(policy: Policy, replace: bool = False) -> Policy:
    """Register ``policy`` in the default registry."""
    return _DEFAULT_REGISTRY.register(policy, replace=replace)


def list_policies() -> List[Policy]:
    """Return every policy registered in the default registry."""
    return _DEFAULT_REGISTRY.all()


def resolve_policy(policy: Optional[object] = None) -> Policy:
    """Resolve ``policy`` into a :class:`Policy` instance.

    ``None`` returns the default policy, a :class:`Policy` is returned
    unchanged, a ``(identifier, version)`` tuple or an ``"identifier@version"``
    string is looked up in the default registry, and a bare identifier
    resolves to its highest registered version.

    Raises:
        UnknownPolicyError: if an explicitly requested policy is not
            registered.  Explicit selection never falls back to the default
            policy silently.
    """
    if policy is None:
        return DEFAULT_POLICY
    if isinstance(policy, Policy):
        return policy
    if isinstance(policy, tuple) and len(policy) == 2:
        identifier, version = policy
        return _DEFAULT_REGISTRY.get(str(identifier), str(version))
    if isinstance(policy, str):
        return _DEFAULT_REGISTRY.resolve(policy)
    raise TypeError(
        "a grading policy must be a Policy, an 'identifier@version' string, "
        f"an (identifier, version) tuple, or None, got {type(policy).__name__}"
    )
