"""Opt-in rule metadata and crash-recoverable streaming."""

import json

import pytest

from makeprov import InPath, OutPath, ProvMeta, ProvenanceConfig, new_session, rule
from makeprov.prov import Prov


def activities(path):
    return [node for node in json.loads(path.read_text())["provenance"]
            if node.get("type") == "prov:Activity"]


def metadata(activity):
    return {item["schema:name"]: item["schema:value"]
            for item in activity.get("schema:additionalProperty", [])}


def test_meta_defaults_opt_in_and_duration(tmp_path):
    session = new_session()
    cfg = ProvenanceConfig(prov_path=str(tmp_path / "provenance"), context=True)

    @rule(config=cfg, session=session)
    def create(out: OutPath, model: ProvMeta[str], api_key: str,
               effort: ProvMeta[str] = "none"):
        out.write_text(model)

    create(OutPath(tmp_path / "out"), "llama", "TOP_SECRET")
    (activity,) = activities(tmp_path / "provenance.json")
    assert metadata(activity) == {"model": "llama", "effort": "none"}
    assert "TOP_SECRET" not in (tmp_path / "provenance.json").read_text()
    assert activity["duration"].startswith("PT")
    assert activity["duration"].endswith("S")
    assert cfg.stream is False


def test_stream_writes_child_live_and_merges_atomically(tmp_path):
    session = new_session()
    path = tmp_path / "provenance" / "run"
    cfg = ProvenanceConfig(prov_path=str(path), stream=True, context=True)
    stream = path.with_suffix(".jsonl")

    @rule(phony=True, session=session, config=cfg)
    def child(index: ProvMeta[int], key: str):
        assert stream.exists()
        assert len(stream.read_text().splitlines()) >= 1

    @rule(config=cfg, session=session)
    def parent(out: OutPath, model: ProvMeta[str]):
        child(0, "SECRET")
        lines = [json.loads(line) for line in stream.read_text().splitlines()]
        assert len(lines) == 2
        assert lines[0]["type"] == "prov:Activity"  # parent start record
        child_activity = next(n for n in lines[1]["provenance"] if n["type"] == "prov:Activity")
        assert metadata(child_activity) == {"index": 0}
        assert child_activity["prov:wasInfluencedBy"] == {"@id": lines[0]["id"]}
        out.write_text("ok")

    parent(OutPath(tmp_path / "output"), model="llama")
    assert not stream.exists()
    assert len(activities(path.with_suffix(".json"))) == 2
    assert metadata(next(a for a in activities(path.with_suffix(".json"))
                         if a.get("schema:additionalProperty") and "model" in metadata(a))) == {"model": "llama"}


def test_failure_keeps_jsonl_without_merged_output(tmp_path):
    session = new_session()
    path = tmp_path / "run"
    cfg = ProvenanceConfig(prov_path=str(path), stream=True)

    @rule(phony=True, session=session, config=cfg)
    def child(index: ProvMeta[int]):
        return None

    @rule(phony=True, session=session, config=cfg)
    def parent():
        child(1)
        raise ValueError("interrupted")

    with pytest.raises(ValueError, match="interrupted"):
        parent()
    lines = [json.loads(line) for line in path.with_suffix(".jsonl").read_text().splitlines()]
    assert len(lines) == 3  # parent start, child complete, parent failed
    assert any(a.get("comment") == "task failed" for a in lines[-1]["provenance"])
    assert not path.with_suffix(".json").exists()
    assert not session.prov_buffers


def test_stream_without_merge_is_final_jsonl(tmp_path):
    session = new_session()
    path = tmp_path / "run"
    cfg = ProvenanceConfig(prov_path=str(path), stream=True, merge=False)

    @rule(phony=True, session=session, config=cfg)
    def step(item: ProvMeta[dict]):
        pass

    step({"x": 3})
    assert path.with_suffix(".jsonl").exists()
    assert not path.with_suffix(".json").exists()
    data = [json.loads(line) for line in path.with_suffix(".jsonl").read_text().splitlines()]
    assert metadata(next(a for a in data[-1]["provenance"] if a["type"] == "prov:Activity")) == {
        "item": {"@value": {"x": 3}, "@type": "@json"}
    }


def test_failed_atomic_merge_preserves_jsonl(tmp_path, monkeypatch):
    session = new_session()
    path = tmp_path / "run"
    cfg = ProvenanceConfig(prov_path=str(path), stream=True)

    @rule(phony=True, session=session, config=cfg)
    def step():
        pass

    def fail_write(*args, **kwargs):
        raise OSError("disk error")

    monkeypatch.setattr(Prov, "write", fail_write)
    with pytest.raises(Exception, match="disk error"):
        step()
    assert path.with_suffix(".jsonl").exists()
    assert not path.with_suffix(".json").exists()
