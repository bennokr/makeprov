import datetime
from makeprov import prov


def _fake_metadata():
    return "example", "1.0.0", ["alpha>=1.0", "beta==2.0"]


def _build_prov(monkeypatch, run_id: str):
    monkeypatch.setattr(prov, "project_metadata", lambda dist_name=None: _fake_metadata())
    return prov.Prov.create(
        base_iri="http://example.org/", 
        name="demo", 
        run_id=run_id, 
        t0=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
        t1=datetime.datetime(2024, 1, 1, 0, 1, tzinfo=datetime.timezone.utc),
        inputs=[],
        outputs=[],
        results=[],
    )


def test_env_id_is_hash_based(monkeypatch):
    first = _build_prov(monkeypatch, "run-a")
    second = _build_prov(monkeypatch, "run-b")

    env_ids_first = [n.id for n in first.provenance if isinstance(n, prov.EnvNode)]
    env_ids_second = [n.id for n in second.provenance if isinstance(n, prov.EnvNode)]

    assert env_ids_first == env_ids_second
    assert env_ids_first[0].startswith("http://example.org/env-")


def test_merge_deduplicates_environment(monkeypatch):
    a = _build_prov(monkeypatch, "run-a")
    b = _build_prov(monkeypatch, "run-b")

    merged = prov.Prov.merge([a, b])
    env_ids = [n.id for n in merged.provenance if isinstance(n, prov.EnvNode)]

    assert len(env_ids) == 1
