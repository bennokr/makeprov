from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from makeprov import prov as prov_mod


def test_prov_context_is_isolated_from_common(monkeypatch):
    before = deepcopy(prov_mod.COMMON_CONTEXT)

    responses = {
        "git rev-parse HEAD": "abc123",
        "git config --get remote.origin.url": "https://github.com/example/repo.git",
        "git rev-parse --abbrev-ref HEAD": "main",
    }

    def fake_safe_cmd(argv: list[str]):
        return responses.get(" ".join(argv))

    monkeypatch.setattr(prov_mod, "_safe_cmd", fake_safe_cmd)

    t0 = datetime.now(timezone.utc)
    prov = prov_mod.Prov.create(
        base_iri=None,
        name="context",
        run_id="run-1",
        t0=t0,
        t1=t0,
        inputs=[],
        outputs=[],
        results=[],
        success=True,
    )

    assert prov_mod.COMMON_CONTEXT == before
    assert prov.context is not prov_mod.COMMON_CONTEXT
    assert prov.context.get("@base") == "https://github.com/example/repo#"
    assert prov.context.get("blob") == "https://github.com/example/repo/blob/main/"
