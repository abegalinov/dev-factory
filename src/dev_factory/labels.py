"""Label catalog and idempotent bootstrap planning."""

from dataclasses import dataclass

from dev_factory.models import Label
from dev_factory.state_machine import Stage

_STAGE_SPECS: dict[Stage, tuple[str, str]] = {
    Stage.SPEC: ("1d76db", "Factory: draft or revise the specification"),
    Stage.SPEC_REVIEW: ("5319e7", "Factory: spec PR awaiting human merge"),
    Stage.IMPLEMENT: ("0e8a16", "Factory: implement the approved spec"),
    Stage.VERIFY: ("fbca04", "Factory: collecting check results"),
    Stage.REVIEW: ("c5def5", "Factory: code PR awaiting human review"),
    Stage.RELEASE: ("006b75", "Factory: release candidate awaiting deploy approval"),
    Stage.NEEDS_INPUT: ("d93f0b", "Factory: waiting for an answer from a maintainer"),
    Stage.BLOCKED: ("b60205", "Factory: cannot proceed; needs human intervention"),
}

METADATA_LABELS: tuple[Label, ...] = (
    Label("type:bug", "d73a4a", "Something is broken"),
    Label("type:feature", "a2eeef", "New capability"),
    Label("type:chore", "cfd3d7", "Maintenance"),
    Label("priority:high", "b60205", "Handle first"),
    Label("priority:medium", "fbca04", "Normal priority"),
    Label("priority:low", "0e8a16", "When convenient"),
)

PRIORITY_ORDER = {"priority:high": 0, "priority:medium": 1, "priority:low": 2}


def catalog(label_map: dict[Stage, str]) -> list[Label]:
    stage_labels = [
        Label(label_map[stage], color, description)
        for stage, (color, description) in _STAGE_SPECS.items()
    ]
    return stage_labels + list(METADATA_LABELS)


@dataclass(frozen=True)
class BootstrapPlan:
    create: tuple[Label, ...]
    drifted: tuple[tuple[Label, Label], ...]  # (expected, actual); reported, never overwritten
    present: tuple[str, ...]


def plan_bootstrap(expected: list[Label], existing: list[Label]) -> BootstrapPlan:
    # GitHub label names are case-insensitive.
    by_name = {label.name.lower(): label for label in existing}
    create, drifted, present = [], [], []
    for label in expected:
        actual = by_name.get(label.name.lower())
        if actual is None:
            create.append(label)
            continue
        present.append(label.name)
        if actual.color.lower() != label.color or actual.description != label.description:
            drifted.append((label, actual))
    return BootstrapPlan(tuple(create), tuple(drifted), tuple(present))


def priority_rank(labels: tuple[str, ...]) -> int:
    return min((PRIORITY_ORDER[name] for name in labels if name in PRIORITY_ORDER), default=1)
