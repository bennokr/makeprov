"""Tests for the 0.7 provenance model: plans, agents and artifact references."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from makeprov import (
    ArtifactRef,
    OutPath,
    InPath,
    ProvenanceConfig,
    UnresolvedArtifactError,
    new_session,
    rule,
)
from makeprov import prov as prov_mod
from makeprov.prov import AgentNode, AssociationNode, FileEntity, PersonNode, PlanNode


def _create(**kwargs):
    defaults = dict(
        base_iri="https://example.org/",
        name="job",
        run_id="run-1",
        t0=datetime.now(timezone.utc),
        t1=datetime.now(timezone.utc),
        inputs=[],
        outputs=[],
        results=[],
        success=True,
    )
    defaults.update(kwargs)
    return prov_mod.Prov.create(**defaults)


def _only(prov, cls):
    return [n for n in prov.provenance if isinstance(n, cls)]


def _entities(prov):
    """All file entities, including inputs nested under ``activity.used``."""

    found = list(_only(prov, FileEntity))
    for node in prov.provenance:
        for used in getattr(node, "used", None) or ():
            if isinstance(used, FileEntity):
                found.append(used)
    return found


def test_plan_is_separate_from_agent(monkeypatch):
    """The script is a prov:Plan, not a prov:Agent.

    Before 0.7 a single node was simultaneously prov:SoftwareAgent and
    schema:SoftwareSourceCode, which left no distinct slot for the plan.
    """

    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    prov = _create()

    (plan,) = _only(prov, PlanNode)
    assert "prov:Plan" in plan.type
    assert "schema:SoftwareSourceCode" in plan.type
    assert "prov:Agent" not in plan.type

    (agent,) = _only(prov, AgentNode)
    assert "prov:SoftwareAgent" in agent.type
    assert "schema:SoftwareSourceCode" not in agent.type


def test_qualified_association_links_agent_to_plan(monkeypatch):
    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    prov = _create()

    (plan,) = _only(prov, PlanNode)
    (assoc,) = _only(prov, AssociationNode)
    activity = next(n for n in prov.provenance if n.type == "prov:Activity")

    assert assoc.hadPlan == plan.id
    assert activity.qualifiedAssociation == assoc.id


def test_person_agent_from_git_config(monkeypatch):
    responses = {
        "git config --get user.name": "Ada Lovelace",
        "git config --get user.email": "ada@example.org",
    }
    monkeypatch.setattr(
        prov_mod, "_safe_cmd", lambda argv: responses.get(" ".join(argv))
    )

    prov = _create()
    (person,) = _only(prov, PersonNode)

    assert person.id == "mailto:ada@example.org"
    assert person.name == "Ada Lovelace"
    assert "schema:Person" in person.type


def test_no_person_node_without_git_identity(monkeypatch):
    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    assert _only(_create(), PersonNode) == []


def test_external_ref_recorded_without_touching_filesystem(monkeypatch):
    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)

    ref = ArtifactRef.external(
        "https://example.org/datasets/train-v17",
        types=("prov:Entity", "schema:Dataset"),
        digest="sha256:deadbeef",
    )
    prov = _create(inputs=[ref])

    entity = next(
        n
        for n in _entities(prov)
        if n.id == "https://example.org/datasets/train-v17"
    )
    assert entity.identifier == "sha256:deadbeef"
    assert "schema:Dataset" in entity.type


def test_outputs_carry_content_digests(monkeypatch, tmp_path):
    """Outputs used to have their sha256 computed and then discarded."""

    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    out = tmp_path / "out.txt"
    out.write_text("hello", encoding="utf-8")

    prov = _create(outputs=[ArtifactRef.local(out)])
    (entity,) = [n for n in _only(prov, FileEntity) if n.id.endswith("out.txt")]

    assert entity.identifier is not None
    assert entity.identifier.startswith("sha256:")


def test_missing_output_on_success_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)

    with pytest.raises(UnresolvedArtifactError, match="declared output"):
        _create(outputs=[ArtifactRef.local(tmp_path / "never-written.txt")])


def test_missing_output_tolerated_on_failure(monkeypatch, tmp_path):
    """A failed rule legitimately has no outputs; that must not mask the failure."""

    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    prov = _create(
        outputs=[ArtifactRef.local(tmp_path / "never-written.txt")], success=False
    )
    assert [n for n in _only(prov, FileEntity) if n.id.endswith("never-written.txt")] == []


def test_missing_input_keeps_the_edge(monkeypatch, tmp_path, caplog):
    """A missing input is recorded without metadata rather than silently dropped."""

    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    with caplog.at_level("WARNING"):
        prov = _create(inputs=[ArtifactRef.local(tmp_path / "absent.txt")])

    (entity,) = [n for n in _entities(prov) if n.id.endswith("absent.txt")]
    assert entity.identifier is None
    assert "does not exist" in caplog.text


def test_run_ids_are_unique_across_runs(tmp_path):
    """Minute-resolution run ids used to collide for repeat runs of one rule."""

    session = new_session()
    cfg = ProvenanceConfig(prov_dir=str(tmp_path / "prov"))
    seen = []

    @rule(name="repeat", config=cfg, session=session)
    def repeat(out: OutPath = OutPath(tmp_path / "o.txt")):
        out.write_text("x")

    from makeprov.core import _run_id

    for _ in range(5):
        seen.append(_run_id(cfg, datetime.now(timezone.utc)))

    assert len(set(seen)) == len(seen)


def test_external_run_id_is_honored():
    cfg = ProvenanceConfig(run_id="ci-job-42")
    from makeprov.core import _run_id

    assert _run_id(cfg, datetime.now(timezone.utc)) == "ci-job-42"


def test_rule_accepts_artifact_ref_parameter(tmp_path):
    session = new_session()
    cfg = ProvenanceConfig(prov_dir=str(tmp_path / "prov"), base_iri="https://ex.org/")
    src = tmp_path / "src.txt"
    src.write_text("data", encoding="utf-8")

    @rule(name="with_ref", config=cfg, session=session)
    def with_ref(
        source: InPath = InPath(src),
        dataset: ArtifactRef = ArtifactRef.external(
            "https://ex.org/datasets/d1", types=("prov:Entity", "schema:Dataset")
        ),
        out: OutPath = OutPath(tmp_path / "out.txt"),
    ):
        out.write_text(source.read_text())

    with_ref()

    written = list((tmp_path / "prov").glob("*"))
    assert written
    assert "https://ex.org/datasets/d1" in written[0].read_text(encoding="utf-8")


def test_merge_deduplicates_singleton_descriptors(monkeypatch):
    """Plan, runtime and user are per-run singletons, not per-rule."""

    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    merged = prov_mod.Prov.merge([_create(name="a"), _create(name="b")])

    assert len(_only(merged, PlanNode)) == 1
    assert len(_only(merged, AgentNode)) == 1
    # Each activity keeps its own association.
    assert len(_only(merged, AssociationNode)) == 2


def test_artifact_ref_requires_id_or_path():
    with pytest.raises(ValueError, match="either an 'id' or a 'path'"):
        ArtifactRef()


def test_artifact_ref_resolve_populates_metadata(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("abc", encoding="utf-8")

    resolved = ArtifactRef.local(target).resolve()

    assert resolved.extent == 3
    assert resolved.digest.startswith("sha256:")
    assert resolved.media_type == "text/plain"


def test_artifact_ref_resolve_is_noop_for_external():
    ref = ArtifactRef.external("https://ex.org/x")
    assert ref.resolve() is ref
