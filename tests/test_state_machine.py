import pytest

from dev_factory.state_machine import (
    ALLOWED,
    DEFAULT_LABELS,
    EXCEPTION_STAGES,
    WORK_STAGES,
    Stage,
    can_transition,
    classify_labels,
    label_map_from_config,
    require_transition,
    resume_target,
)

# The happy path from docs/issue-contract.md, in order.
HAPPY_PATH = [
    Stage.SPEC, Stage.SPEC_REVIEW, Stage.IMPLEMENT, Stage.VERIFY, Stage.REVIEW, Stage.RELEASE,
]

# Every allowed edge, written out independently of ALLOWED so changes to the table are deliberate.
EXPECTED_EDGES = {
    (Stage.SPEC, Stage.SPEC_REVIEW), (Stage.SPEC, Stage.NEEDS_INPUT), (Stage.SPEC, Stage.BLOCKED),
    # Small tasks (agent.fast_track): the code PR carries the spec; the engine decides when.
    (Stage.SPEC, Stage.IMPLEMENT),
    (Stage.SPEC_REVIEW, Stage.IMPLEMENT), (Stage.SPEC_REVIEW, Stage.SPEC),
    (Stage.SPEC_REVIEW, Stage.BLOCKED),
    (Stage.IMPLEMENT, Stage.VERIFY), (Stage.IMPLEMENT, Stage.NEEDS_INPUT),
    (Stage.IMPLEMENT, Stage.BLOCKED),
    (Stage.VERIFY, Stage.REVIEW), (Stage.VERIFY, Stage.IMPLEMENT), (Stage.VERIFY, Stage.BLOCKED),
    (Stage.REVIEW, Stage.RELEASE), (Stage.REVIEW, Stage.VERIFY), (Stage.REVIEW, Stage.IMPLEMENT),
    (Stage.REVIEW, Stage.BLOCKED),
    (Stage.RELEASE, Stage.BLOCKED),
    (Stage.NEEDS_INPUT, Stage.BLOCKED),
    *((Stage.NEEDS_INPUT, s) for s in WORK_STAGES),
    *((Stage.BLOCKED, s) for s in WORK_STAGES),
}


def test_transition_table_matches_contract():
    actual = {(src, dst) for src, targets in ALLOWED.items() for dst in targets}
    assert actual == EXPECTED_EDGES


@pytest.mark.parametrize("src,dst", list(zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False)))
def test_happy_path_is_allowed(src, dst):
    require_transition(src, dst)


@pytest.mark.parametrize("src", list(Stage))
def test_no_self_transitions(src):
    assert not can_transition(src, src)


@pytest.mark.parametrize("src", sorted(WORK_STAGES))
def test_every_work_stage_can_block(src):
    assert can_transition(src, Stage.BLOCKED)


def test_human_gates_cannot_be_skipped():
    # spec -> implement exists only for small tasks, whose code PR carries the spec: the
    # human still approves the spec, together with the code (see test_integration).
    assert not can_transition(Stage.IMPLEMENT, Stage.REVIEW)
    assert not can_transition(Stage.VERIFY, Stage.RELEASE)


def test_release_only_exits_to_blocked():
    assert ALLOWED[Stage.RELEASE] == {Stage.BLOCKED}


def test_invalid_transition_raises():
    with pytest.raises(ValueError):
        require_transition(Stage.SPEC, Stage.RELEASE)


def test_resume_target():
    assert resume_target(Stage.NEEDS_INPUT, Stage.IMPLEMENT) is Stage.IMPLEMENT
    with pytest.raises(ValueError):
        resume_target(Stage.NEEDS_INPUT, Stage.REVIEW)
    with pytest.raises(ValueError):
        resume_target(Stage.NEEDS_INPUT, None)
    with pytest.raises(ValueError):
        resume_target(Stage.BLOCKED, Stage.SPEC)


def test_default_labels():
    assert DEFAULT_LABELS[Stage.SPEC_REVIEW] == "factory:spec-review"
    assert DEFAULT_LABELS[Stage.NEEDS_INPUT] == "factory:needs-input"
    assert {Stage.NEEDS_INPUT, Stage.BLOCKED} == EXCEPTION_STAGES


def test_classify_labels():
    state = classify_labels(["type:bug", "factory:verify"], DEFAULT_LABELS)
    assert state.stage is Stage.VERIFY and not state.is_conflicting

    none = classify_labels(["type:bug"], DEFAULT_LABELS)
    assert not none.is_factory_issue and none.stage is None

    both = classify_labels(["factory:spec", "factory:blocked"], DEFAULT_LABELS)
    assert both.is_conflicting and both.stage is None
    assert both.stages == (Stage.SPEC, Stage.BLOCKED)


def test_label_map_from_config():
    labels = label_map_from_config({"spec": "ai:spec"})
    assert labels[Stage.SPEC] == "ai:spec"
    assert labels[Stage.VERIFY] == "factory:verify"
    with pytest.raises(ValueError):
        label_map_from_config({"specs": "x"})
    with pytest.raises(ValueError):
        label_map_from_config({"spec": "factory:verify"})
