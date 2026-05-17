"""Gate profiles for advisory/blocking evaluation.

All numeric thresholds come from the shared ``code_redline`` package —
this module is a thin adapter that wires those values into the local
``GateThresholds`` dataclass. Do NOT hardcode any threshold number
here; change it in ``code-redline`` (with a MAJOR version bump)
instead.

``strict`` is kept only as a CLI/profile-name compatibility alias. It
uses the same canonical blocking redline as ``blocking``.
"""

from __future__ import annotations

from code_redline import REDLINE

from kodawari.gate.models import GateMode, GateProfile, GateThresholds


def _build_redline_thresholds(*, severity: str, complexity_max: int) -> GateThresholds:
    """Assemble a ``GateThresholds`` from the canonical code_redline values.

    ``complexity_max`` is passed in because advisory mode uses the warn
    band (``REDLINE.complexity_warn``) while blocking uses the block band
    (``REDLINE.complexity_block``). ``function_max_lines`` stays at a
    permissive 10000 because code_redline intentionally does not gate
    function length — only cyclomatic complexity.
    """
    return GateThresholds(
        file_max_lines=REDLINE.file_complexity_block_lines,
        function_max_lines=10000,
        nesting_max=REDLINE.nesting_max,
        complexity_max=complexity_max,
        complexity_warn=REDLINE.complexity_warn,
        complexity_block=REDLINE.complexity_block,
        file_complexity_warn_lines=REDLINE.file_complexity_warn_lines,
        file_complexity_warn_sum=REDLINE.file_complexity_warn_sum,
        file_complexity_block_lines=REDLINE.file_complexity_block_lines,
        file_complexity_block_sum=REDLINE.file_complexity_block_sum,
        max_violations=REDLINE.max_violations,
        severity=severity,
    )


ADVISORY_THRESHOLDS = _build_redline_thresholds(
    severity="WARNING",
    complexity_max=REDLINE.complexity_warn,
)
DEFAULT_THRESHOLDS = _build_redline_thresholds(
    severity="ERROR",
    complexity_max=REDLINE.complexity_block,
)
STRICT_THRESHOLDS = DEFAULT_THRESHOLDS

PROFILES: dict[str, GateProfile] = {
    "advisory": GateProfile(
        name="advisory",
        mode=GateMode.ADVISORY,
        thresholds=ADVISORY_THRESHOLDS,
        description="Non-blocking profile using warn-tier complexity thresholds.",
    ),
    "blocking": GateProfile(
        name="blocking",
        mode=GateMode.BLOCKING,
        thresholds=DEFAULT_THRESHOLDS,
        description="Blocking profile using tiered complexity thresholds.",
    ),
    "tiered": GateProfile(
        name="tiered",
        mode=GateMode.BLOCKING,
        thresholds=DEFAULT_THRESHOLDS,
        description="Explicit alias for tiered blocking redlines.",
    ),
    "strict": GateProfile(
        name="strict",
        mode=GateMode.BLOCKING,
        thresholds=STRICT_THRESHOLDS,
        description="Compatibility alias for the canonical blocking redline.",
    ),
}


def get_profile(profile_name: str | None = None) -> GateProfile:
    normalized = str(profile_name or "advisory").strip().lower()
    if normalized not in PROFILES:
        supported = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unsupported gate profile: {normalized}. Supported: {supported}")
    return PROFILES[normalized]


def list_profiles() -> list[str]:
    return sorted(PROFILES)
