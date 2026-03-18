import json
from pathlib import Path

import pytest

from makeprov import CachedDownload, OutPath, ProvenanceConfig, rule, span
from makeprov.prov import Prov


def test_span_scopes_provenance(monkeypatch, tmp_path):
    original = ProvenanceConfig(**vars(ProvenanceConfig.get()))
    try:
        prov_dir = tmp_path / "prov"
        ProvenanceConfig.set(ProvenanceConfig(prov_dir=str(prov_dir)))

        @rule(name="span_rule")
        def span_rule(out: OutPath = OutPath(tmp_path / "span-out.txt")):
            out.write_text("ok")

        monkeypatch.chdir(tmp_path)

        with span("span-run"):
            span_rule()

        prov_files = list(prov_dir.glob("span-run*.json"))
        assert prov_files

        prov_json = json.loads(prov_files[0].read_text())
        assert prov_json["provenance"]
    finally:
        ProvenanceConfig.set(original)


def test_span_returns_prov_and_writes_when_nested(monkeypatch, tmp_path):
    original = ProvenanceConfig(**vars(ProvenanceConfig.get()))
    try:
        prov_dir = tmp_path / "prov"
        ProvenanceConfig.set(ProvenanceConfig(prov_dir=str(prov_dir)))

        @rule(name="nested_span_rule")
        def nested_span_rule(out: OutPath = OutPath(tmp_path / "nested-out.txt")):
            out.write_text("ok")

        monkeypatch.chdir(tmp_path)

        with span("outer-span"):
            nested_path = tmp_path / "per-model" / "model-a"
            with span("model-a", prov_path=nested_path) as sp:
                nested_span_rule()

            assert isinstance(sp.prov, Prov)
            assert sp.prov.name == "model-a"
            assert nested_path.with_suffix(".json").exists()
    finally:
        ProvenanceConfig.set(original)


def test_cached_download_records_source(monkeypatch, tmp_path):
    original = ProvenanceConfig(**vars(ProvenanceConfig.get()))
    try:
        prov_dir = tmp_path / "prov"
        ProvenanceConfig.set(ProvenanceConfig(prov_dir=str(prov_dir)))

        url = "https://example.com/data.json"
        cache_path = tmp_path / "cache.json"

        def fake_download(src, dest, headers=None, **_):
            Path(dest).write_text('{"ok": true}')

        monkeypatch.setattr("makeprov.paths.download_file", fake_download)

        @rule(name="cached_input", phony=True)
        def cached_input(meta: CachedDownload = CachedDownload(url, str(cache_path))):
            with meta.open() as handle:
                return handle.read()

        monkeypatch.chdir(tmp_path)
        cached_input()

        prov_files = list(prov_dir.glob("cached_input*.json"))
        assert prov_files
        prov_json = json.loads(prov_files[0].read_text())
        activities = [node for node in prov_json["provenance"] if node.get("type") == "prov:Activity"]
        assert activities
        used_entities = []
        for act in activities:
            used_entities.extend([u for u in act.get("used", []) if isinstance(u, dict)])
        assert any(
            e.get("rdfs:seeAlso") == url or e.get("prov:wasDerivedFrom") == url
            for e in used_entities
        )
    finally:
        ProvenanceConfig.set(original)
