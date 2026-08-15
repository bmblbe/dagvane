"""Structural council contract: isolation, self-review exclusion, hard barrier.

These are the N-tests of acceptance criteria 4–6: the checks operate on the
InputManifest structure (producers, dependencies), not on prompt text.
"""

from __future__ import annotations

import dataclasses

import pytest

from dagvane.application.council import CouncilTemplate, PlanValidator
from dagvane.domain.models import (
    Budget,
    BudgetOverrides,
    InputManifest,
    ManifestEntry,
    ModelRoute,
    Plan,
    PlanNode,
    PlanValidationError,
    TaskSpec,
)


def _template() -> tuple[Plan, dict[str, ModelRoute], Budget]:
    task = TaskSpec(
        task_id="t-x",
        title="T",
        statement="S",
        acceptance_criteria=(),
        budget_overrides=BudgetOverrides(),
    )
    return CouncilTemplate.build(task)


def _replace_node(plan: Plan, node_id: str, **changes: object) -> Plan:
    nodes = tuple(
        dataclasses.replace(node, **changes) if node.node_id == node_id else node  # type: ignore[arg-type]
        for node in plan.nodes
    )
    return dataclasses.replace(plan, nodes=nodes)


def test_council_template_is_valid() -> None:
    plan, routes, budget = _template()
    PlanValidator().validate(plan, routes)
    assert budget.max_calls == 60
    assert plan.anonymization == {
        "candidate-1": "proposer-a",
        "candidate-2": "proposer-b",
    }


def test_proposer_isolation_rejects_injected_sibling() -> None:
    plan, routes, _ = _template()
    proposer = next(n for n in plan.nodes if n.node_id == "proposer-a")
    injected = InputManifest(
        entries=(
            *proposer.input_manifest.entries,
            ManifestEntry(kind="proposal", label="candidate-2", producer="proposer-b"),
        )
    )
    # A bare injection without a dependency edge is caught by the ancestor rule.
    mutated = _replace_node(plan, "proposer-a", input_manifest=injected)
    with pytest.raises(PlanValidationError, match="not an ancestor"):
        PlanValidator().validate(mutated, routes)
    # Even WITH a dependency edge, a proposer context may never contain a proposal.
    chained = _replace_node(
        plan, "proposer-a", input_manifest=injected, depends_on=("proposer-b",)
    )
    with pytest.raises(PlanValidationError, match="proposer .* may only contain the task"):
        PlanValidator().validate(chained, routes)


def test_self_review_structurally_impossible() -> None:
    plan, routes, _ = _template()
    # review-by-a (identity A) is redirected to its own identity's proposal.
    self_review = InputManifest(
        entries=(
            ManifestEntry(kind="task", label="task", producer=None),
            ManifestEntry(kind="proposal", label="candidate-1", producer="proposer-a"),
        )
    )
    mutated = _replace_node(plan, "review-by-a", input_manifest=self_review)
    with pytest.raises(PlanValidationError, match="self-review"):
        PlanValidator().validate(mutated, routes)


def test_barrier_requires_dependency_on_all_proposers() -> None:
    plan, routes, _ = _template()
    # Drop the proposer the review does NOT read, so only the barrier rule can fire.
    mutated = _replace_node(plan, "review-by-a", depends_on=("proposer-b",))
    with pytest.raises(PlanValidationError, match="hard barrier"):
        PlanValidator().validate(mutated, routes)


def test_manifest_reference_must_be_behind_the_barrier() -> None:
    plan, routes, _ = _template()
    mutated = _replace_node(plan, "review-by-a", depends_on=("proposer-a",))
    with pytest.raises(PlanValidationError):
        PlanValidator().validate(mutated, routes)


def test_judge_must_depend_on_all_reviews() -> None:
    plan, routes, _ = _template()
    judge = next(n for n in plan.nodes if n.node_id == "judge")
    # Also drop the manifest entry produced by the removed dependency so that
    # only the judge barrier rule can fire.
    pruned = InputManifest(
        entries=tuple(
            e for e in judge.input_manifest.entries if e.producer != "review-by-b"
        )
    )
    mutated = _replace_node(
        plan, "judge", depends_on=("review-by-a",), input_manifest=pruned
    )
    with pytest.raises(PlanValidationError, match="judge .* must depend on all reviews"):
        PlanValidator().validate(mutated, routes)


def test_cycles_rejected() -> None:
    plan, routes, _ = _template()
    mutated = _replace_node(plan, "proposer-a", depends_on=("judge",))
    with pytest.raises(PlanValidationError):
        PlanValidator().validate(mutated, routes)


def test_unknown_route_rejected() -> None:
    plan, routes, _ = _template()
    mutated = _replace_node(plan, "judge", route_id="fake/nonexistent")
    with pytest.raises(PlanValidationError, match="unknown route"):
        PlanValidator().validate(mutated, routes)


def test_anonymization_mapping_must_match_manifests() -> None:
    plan, routes, _ = _template()
    swapped = dataclasses.replace(
        plan,
        anonymization={"candidate-1": "proposer-b", "candidate-2": "proposer-a"},
    )
    with pytest.raises(PlanValidationError, match="anonymization"):
        PlanValidator().validate(swapped, routes)


def test_council_requires_two_proposers_and_one_judge() -> None:
    plan, routes, _ = _template()
    only_one_proposer = dataclasses.replace(
        plan, nodes=tuple(n for n in plan.nodes if n.node_id != "proposer-b")
    )
    with pytest.raises(PlanValidationError):
        PlanValidator().validate(only_one_proposer, routes)

    def strip_judge(node: PlanNode) -> bool:
        return node.node_id != "judge"

    no_judge = dataclasses.replace(plan, nodes=tuple(filter(strip_judge, plan.nodes)))
    with pytest.raises(PlanValidationError, match="exactly one judge"):
        PlanValidator().validate(no_judge, routes)


def test_third_proposer_rejected() -> None:
    plan, routes, _ = _template()
    extra = dataclasses.replace(
        next(n for n in plan.nodes if n.node_id == "proposer-a"),
        node_id="proposer-c",
        identity="C",
        route_id="fake/proposer-a",
    )
    widened = dataclasses.replace(plan, nodes=(*plan.nodes, extra))
    with pytest.raises(PlanValidationError, match="exactly two proposers"):
        PlanValidator().validate(widened, routes)


def test_missing_reviewer_rejected() -> None:
    plan, routes, _ = _template()
    judge = next(n for n in plan.nodes if n.node_id == "judge")
    pruned_manifest = InputManifest(
        entries=tuple(e for e in judge.input_manifest.entries if e.producer != "review-by-b")
    )
    nodes = tuple(
        dataclasses.replace(n, depends_on=("review-by-a",), input_manifest=pruned_manifest)
        if n.node_id == "judge"
        else n
        for n in plan.nodes
        if n.node_id != "review-by-b"
    )
    one_reviewer = dataclasses.replace(plan, nodes=nodes)
    with pytest.raises(PlanValidationError, match="exactly two reviewers"):
        PlanValidator().validate(one_reviewer, routes)


def test_reviewer_identity_outside_proposer_identities_rejected() -> None:
    plan, routes, _ = _template()
    mutated = _replace_node(plan, "review-by-a", identity="C")
    with pytest.raises(PlanValidationError, match="reviewer identities"):
        PlanValidator().validate(mutated, routes)


def test_empty_proposer_context_rejected() -> None:
    plan, routes, _ = _template()
    mutated = _replace_node(plan, "proposer-a", input_manifest=InputManifest(entries=()))
    with pytest.raises(PlanValidationError, match="exactly one task"):
        PlanValidator().validate(mutated, routes)


def test_duplicated_task_entries_in_proposer_rejected() -> None:
    plan, routes, _ = _template()
    doubled = InputManifest(
        entries=(
            ManifestEntry(kind="task", label="task", producer=None),
            ManifestEntry(kind="task", label="task-again", producer=None),
        )
    )
    mutated = _replace_node(plan, "proposer-a", input_manifest=doubled)
    with pytest.raises(PlanValidationError, match="exactly one task"):
        PlanValidator().validate(mutated, routes)


def test_reviewer_seeing_both_proposals_rejected() -> None:
    plan, routes, _ = _template()
    both = InputManifest(
        entries=(
            ManifestEntry(kind="task", label="task", producer=None),
            ManifestEntry(kind="proposal", label="candidate-1", producer="proposer-a"),
            ManifestEntry(kind="proposal", label="candidate-2", producer="proposer-b"),
        )
    )
    mutated = _replace_node(plan, "review-by-a", input_manifest=both)
    with pytest.raises(PlanValidationError, match="exactly the task plus one"):
        PlanValidator().validate(mutated, routes)


def test_judge_missing_a_review_entry_rejected() -> None:
    plan, routes, _ = _template()
    judge = next(n for n in plan.nodes if n.node_id == "judge")
    pruned = InputManifest(
        entries=tuple(e for e in judge.input_manifest.entries if e.producer != "review-by-b")
    )
    mutated = _replace_node(plan, "judge", input_manifest=pruned)
    with pytest.raises(PlanValidationError, match="every proposal, and every review"):
        PlanValidator().validate(mutated, routes)


def test_conflicting_proposal_labels_across_manifests_rejected() -> None:
    plan, routes, _ = _template()
    relabeled = InputManifest(
        entries=(
            ManifestEntry(kind="task", label="task", producer=None),
            # review-by-b reviews proposer-a's proposal but under the label the
            # rest of the council uses for proposer-b's proposal.
            ManifestEntry(kind="proposal", label="candidate-2", producer="proposer-a"),
        )
    )
    mutated = _replace_node(plan, "review-by-b", input_manifest=relabeled)
    with pytest.raises(PlanValidationError, match="conflicting producers"):
        PlanValidator().validate(mutated, routes)


def test_extra_anonymization_alias_rejected() -> None:
    plan, routes, _ = _template()
    ghosted = dataclasses.replace(
        plan,
        anonymization={**plan.anonymization, "candidate-ghost": "proposer-a"},
    )
    with pytest.raises(PlanValidationError, match="exactly the proposal labels in use"):
        PlanValidator().validate(ghosted, routes)


def test_missing_anonymization_alias_rejected() -> None:
    plan, routes, _ = _template()
    partial = dataclasses.replace(plan, anonymization={"candidate-1": "proposer-a"})
    with pytest.raises(PlanValidationError, match="exactly the proposal labels in use"):
        PlanValidator().validate(partial, routes)


def test_sealed_mapping_is_deeply_immutable() -> None:
    plan, _, _ = _template()
    mapping = plan.anonymization
    with pytest.raises(TypeError):
        mapping["candidate-ghost"] = "proposer-a"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.anonymization = {}  # type: ignore[misc]
    # replace() reseals: the new plan's mapping is again an immutable view.
    replaced = dataclasses.replace(plan, anonymization=dict(plan.anonymization))
    with pytest.raises(TypeError):
        replaced.anonymization["candidate-ghost"] = "proposer-a"  # type: ignore[index]
