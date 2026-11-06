"""Lifecycle stages and allowed transitions. See docs/issue-contract.md."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    SPEC = "spec"
    SPEC_REVIEW = "spec_review"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REVIEW = "review"
    RELEASE = "release"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"


EXCEPTION_STAGES = frozenset({Stage.NEEDS_INPUT, Stage.BLOCKED})
WORK_STAGES = frozenset(Stage) - EXCEPTION_STAGES
# Stages where the next step belongs to a human; the factory only watches for evidence.
HUMAN_GATES = frozenset({Stage.SPEC_REVIEW, Stage.REVIEW, Stage.RELEASE})

DEFAULT_LABELS: dict[Stage, str] = {
    stage: "factory:" + stage.value.replace("_", "-") for stage in Stage
}

# Transitions that require proof (a merged PR, passing checks, an authorized actor)
# must additionally be validated by the caller against live GitHub state.
ALLOWED: dict[Stage, frozenset[Stage]] = {
    # spec -> implement: a small task (agent.fast_track) has no spec PR; its code PR carries
    # the spec, and merging it approves both.
    Stage.SPEC: frozenset({Stage.SPEC_REVIEW, Stage.IMPLEMENT, Stage.NEEDS_INPUT,
                           Stage.BLOCKED}),
    Stage.SPEC_REVIEW: frozenset({Stage.IMPLEMENT, Stage.SPEC, Stage.BLOCKED}),
    Stage.IMPLEMENT: frozenset({Stage.VERIFY, Stage.NEEDS_INPUT, Stage.BLOCKED}),
    Stage.VERIFY: frozenset({Stage.REVIEW, Stage.IMPLEMENT, Stage.BLOCKED}),
    Stage.REVIEW: frozenset({Stage.RELEASE, Stage.VERIFY, Stage.IMPLEMENT, Stage.BLOCKED}),
    Stage.RELEASE: frozenset({Stage.BLOCKED}),
    # A human may resume an exception state into any work stage; the factory itself
    # only resumes needs-input into the marker's resume_stage (see resume_target).
    Stage.NEEDS_INPUT: WORK_STAGES | {Stage.BLOCKED},
    Stage.BLOCKED: WORK_STAGES,
}


def can_transition(current: Stage, target: Stage) -> bool:
    return target in ALLOWED[current]


def require_transition(current: Stage, target: Stage) -> None:
    if not can_transition(current, target):
        raise ValueError(f"Invalid workflow transition: {current.value} -> {target.value}")


def resume_target(current: Stage, resume_stage: Stage | None) -> Stage:
    """The only stage the factory may automatically resume needs-input into."""
    if current is not Stage.NEEDS_INPUT:
        raise ValueError(f"Automatic resume is only defined for needs_input, not {current.value}")
    if resume_stage not in (Stage.SPEC, Stage.IMPLEMENT):
        raise ValueError(f"Invalid resume stage for needs_input: {resume_stage}")
    return resume_stage


@dataclass(frozen=True)
class LabelState:
    """Lifecycle interpretation of an issue's labels."""

    stages: tuple[Stage, ...]

    @property
    def stage(self) -> Stage | None:
        return self.stages[0] if len(self.stages) == 1 else None

    @property
    def is_factory_issue(self) -> bool:
        return bool(self.stages)

    @property
    def is_conflicting(self) -> bool:
        return len(self.stages) > 1


def classify_labels(labels: Iterable[str], label_map: Mapping[Stage, str]) -> LabelState:
    by_label = {name: stage for stage, name in label_map.items()}
    found = {by_label[name] for name in labels if name in by_label}
    return LabelState(tuple(stage for stage in Stage if stage in found))


def label_map_from_config(labels: Mapping[str, str]) -> dict[Stage, str]:
    """Merge per-project label overrides (keyed by stage value) over the defaults."""
    unknown = set(labels) - {stage.value for stage in Stage}
    if unknown:
        raise ValueError(f"Unknown workflow label keys: {sorted(unknown)}")
    merged = {stage: labels.get(stage.value, default) for stage, default in DEFAULT_LABELS.items()}
    if len(set(merged.values())) != len(merged):
        raise ValueError("Workflow labels must be distinct")
    return merged
