import datetime

from makeprov import prov


def _fake_metadata():
    return "example", "1.0.0", ["alpha>=1.0"]


def _build_prov(monkeypatch, *, record_environment: bool):
    monkeypatch.setattr(prov, "project_metadata", lambda dist_name=None: _fake_metadata())
    return prov.Prov.create(
        base_iri="http://example.org/",
        name="demo",
        run_id="run-a",
        t0=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
        t1=datetime.datetime(2024, 1, 1, 0, 1, tzinfo=datetime.timezone.utc),
        inputs=[],
        outputs=[],
        results=[],
        record_environment=record_environment,
    )


def _env_node(result):
    envs = [n for n in result.provenance if isinstance(n, prov.EnvNode)]
    assert len(envs) == 1
    return envs[0]


def test_record_environment_off_by_default_omits_resolved(monkeypatch):
    result = _build_prov(monkeypatch, record_environment=False)
    env = _env_node(result)

    assert env.resolved is None
    assert env.wasDerivedFrom is None


def test_record_environment_populates_resolved_from_imported_distributions(monkeypatch):
    monkeypatch.setattr(prov, "_installed_distributions", lambda: ["pytest==8.0.0", "unused-pkg==1.0"])
    monkeypatch.setattr(prov, "_imported_distributions", lambda installed: ["pytest==8.0.0"])
    monkeypatch.setattr(prov, "_find_lockfile", lambda *dirs: None)

    result = _build_prov(monkeypatch, record_environment=True)
    env = _env_node(result)

    assert env.resolved is not None
    labels = [dep.label for dep in env.resolved]
    assert labels == ["pytest==8.0.0"]
    assert env.wasDerivedFrom is None


def test_record_environment_cites_lockfile_when_found(monkeypatch, tmp_path):
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("# fake lock\n")

    monkeypatch.setattr(prov, "_installed_distributions", lambda: [])
    monkeypatch.setattr(prov, "_imported_distributions", lambda installed: [])
    monkeypatch.setattr(prov, "_find_lockfile", lambda *dirs: lockfile)

    result = _build_prov(monkeypatch, record_environment=True)
    env = _env_node(result)
    lock_entities = [n for n in result.provenance if isinstance(n, prov.FileEntity) and n.id == env.wasDerivedFrom]

    assert env.wasDerivedFrom is not None
    assert len(lock_entities) == 1
    assert lock_entities[0].sha256 is not None


def test_agent_records_operating_system(monkeypatch):
    result = _build_prov(monkeypatch, record_environment=False)
    agents = [n for n in result.provenance if isinstance(n, prov.AgentNode)]

    assert len(agents) == 1
    assert agents[0].operatingSystem


def test_installed_distributions_returns_name_version_pairs():
    installed = prov._installed_distributions()

    assert installed
    assert all("==" in spec for spec in installed)


def test_find_lockfile_prefers_first_match(tmp_path):
    (tmp_path / "poetry.lock").write_text("")
    (tmp_path / "uv.lock").write_text("")

    found = prov._find_lockfile(tmp_path)

    assert found is not None
    assert found.name == "uv.lock"


def test_find_lockfile_returns_none_when_absent(tmp_path):
    assert prov._find_lockfile(tmp_path) is None
