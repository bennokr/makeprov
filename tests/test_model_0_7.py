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


def _git_identity(monkeypatch):
    responses = {
        "git config --get user.name": "Ada Lovelace",
        "git config --get user.email": "ada@example.org",
    }
    monkeypatch.setattr(
        prov_mod, "_safe_cmd", lambda argv: responses.get(" ".join(argv))
    )


def test_person_agent_recorded_when_opted_in(monkeypatch):
    _git_identity(monkeypatch)

    prov = _create(record_user=True)
    (person,) = _only(prov, PersonNode)

    assert person.id == "mailto:ada@example.org"
    assert person.name == "Ada Lovelace"
    assert "schema:Person" in person.type


def test_person_is_opt_in(monkeypatch):
    """A git identity alone must not put personal data in the document."""

    _git_identity(monkeypatch)

    prov = _create()
    assert _only(prov, PersonNode) == []

    serialized = str(prov.to_jsonld())
    assert "ada@example.org" not in serialized
    assert "Ada Lovelace" not in serialized


def test_association_agent_falls_back_to_runtime(monkeypatch):
    _git_identity(monkeypatch)

    prov = _create()
    (assoc,) = _only(prov, AssociationNode)
    (agent,) = _only(prov, AgentNode)

    assert assoc.agent == agent.id


def test_no_person_node_without_git_identity(monkeypatch):
    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    assert _only(_create(record_user=True), PersonNode) == []


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


def test_outputs_expose_bare_hex_checksum(monkeypatch, tmp_path):
    """schema:sha256 alongside dct:identifier, so no prefix parsing is needed."""

    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    out = tmp_path / "out.txt"
    out.write_text("hello", encoding="utf-8")

    prov = _create(outputs=[ArtifactRef.local(out)])
    (entity,) = [n for n in _only(prov, FileEntity) if n.id.endswith("out.txt")]

    assert entity.identifier == f"sha256:{entity.sha256}"
    assert len(entity.sha256) == 64
    assert ":" not in entity.sha256


def test_non_sha256_digest_leaves_sha256_unset(monkeypatch):
    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    ref = ArtifactRef.external("https://ex.org/x", digest="md5:abc")

    prov = _create(inputs=[ref])
    (entity,) = [n for n in _entities(prov) if n.id == "https://ex.org/x"]

    assert entity.identifier == "md5:abc"
    assert entity.sha256 is None


def test_activity_states_generated_forward(monkeypatch, tmp_path):
    """prov:used and prov:generated are the pair read off the activity."""

    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    out = tmp_path / "out.txt"
    out.write_text("x", encoding="utf-8")

    prov = _create(outputs=[ArtifactRef.local(out)])
    activity = next(n for n in prov.provenance if n.type == "prov:Activity")
    (entity,) = [n for n in _only(prov, FileEntity) if n.id.endswith("out.txt")]

    assert activity.generated == (entity.id,)
    assert entity.wasGeneratedBy == activity.id


def test_dirty_tree_marks_plan_version(monkeypatch, caplog):
    """A bare SHA beside a modified tree would name code that isn't what ran."""

    responses = {
        "git rev-parse HEAD": "abc123",
        "git status --porcelain": " M src/makeprov/core.py",
    }
    monkeypatch.setattr(
        prov_mod, "_safe_cmd", lambda argv: responses.get(" ".join(argv))
    )

    with caplog.at_level("WARNING"):
        prov = _create()

    (plan,) = _only(prov, PlanNode)
    assert plan.hasVersion == "abc123-dirty"
    assert "uncommitted changes" in caplog.text


def test_clean_tree_records_bare_commit(monkeypatch):
    responses = {"git rev-parse HEAD": "abc123", "git status --porcelain": ""}
    monkeypatch.setattr(
        prov_mod, "_safe_cmd", lambda argv: responses.get(" ".join(argv))
    )

    (plan,) = _only(_create(), PlanNode)
    assert plan.hasVersion == "abc123"


def test_repl_script_name_is_iri_safe(monkeypatch):
    """Notebooks and REPLs yield names like "<stdin>" that break IRIs."""

    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    monkeypatch.setattr(prov_mod, "_caller_script", lambda: Path("<stdin>"))

    prov = _create()
    ids = [n.id for n in prov.provenance]

    assert not any("<" in i or ">" in i for i in ids)
    assert any("%3Cstdin%3E" in i for i in ids)


def test_plan_graph_is_off_by_default(monkeypatch):
    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    prov = _create()

    (plan,) = _only(prov, PlanNode)
    (assoc,) = _only(prov, AssociationNode)
    assert plan.requires is None
    assert assoc.hadPlan == plan.id


def test_plan_graph_links_rules_with_requires(monkeypatch):
    monkeypatch.setattr(prov_mod, "_safe_cmd", lambda argv: None)
    prov = _create(plan_graph=prov_mod.PlanGraph(rule="transform", requires=("extract",)))

    plans = {p.label: p for p in _only(prov, PlanNode)}
    rule_plan = plans["transform"]
    (assoc,) = _only(prov, AssociationNode)

    assert rule_plan.requires is not None
    assert rule_plan.requires[0].endswith("#rule-extract")
    # The rule is the more precise plan the activity carried out.
    assert assoc.hadPlan == rule_plan.id


def test_plan_graph_end_to_end_uses_the_build_resolver(tmp_path):
    """The emitted structure must agree with what build() would actually do."""

    session = new_session()
    cfg = ProvenanceConfig(
        prov_dir=str(tmp_path / "prov"),
        base_iri="https://ex.org/",
        emit_plan_graph=True,
    )
    raw = tmp_path / "raw.txt"
    clean = tmp_path / "clean.txt"

    @rule(name="extract", config=cfg, session=session)
    def extract(out: OutPath = OutPath(raw)):
        out.write_text("raw")

    @rule(name="transform", config=cfg, session=session)
    def transform(src: InPath = InPath(raw), out: OutPath = OutPath(clean)):
        out.write_text(src.read_text().upper())

    extract()
    transform()

    document = (tmp_path / "prov" / "transform.json").read_text(encoding="utf-8")
    assert "#rule-transform" in document
    # transform depends on a file that `extract` produces, so the resolver
    # should have found that edge.
    assert "#rule-extract" in document


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
