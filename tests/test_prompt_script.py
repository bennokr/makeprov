"""Exercise the shortened example without a model server or the optional llm package."""

import importlib.util
import json
import sys
import types
from pathlib import Path

from makeprov import ProvenanceConfig


def test_prompt_example(tmp_path, monkeypatch):
    llm = types.ModuleType("llm")
    plugins = types.ModuleType("llm.default_plugins")
    openai = types.ModuleType("llm.default_plugins.openai_models")
    utils = types.ModuleType("llm.utils")

    class Chat:
        def __init__(self, **kwargs):
            self.settings = kwargs

        def prompt(self, prompt, **kwargs):
            assert kwargs["key"] == "sk-unsloth-SECRET"
            assert kwargs["options"] == {"reasoning_effort": "none"}
            return types.SimpleNamespace(
                text=lambda: '```json\n{"valid": true}\n```',
                response_json={"choices": [{"message": {"reasoning_content": "checked"}}]},
            )

    openai.Chat = Chat
    utils.extract_fenced_code_block = lambda text: text[7:-3]
    for name, module in {
        "llm": llm, "llm.default_plugins": plugins,
        "llm.default_plugins.openai_models": openai, "llm.utils": utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    source = Path(__file__).resolve().parents[1] / "examples" / "prompt.py"
    spec = importlib.util.spec_from_file_location("prompt_example", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_model_id", lambda key: "llama-test")

    data = tmp_path / "input.jsonl"
    data.write_text('{"id": "one"}\n\n{"id": "two"}\n')
    template = tmp_path / "template.txt"
    template.write_text("Annotate this:")
    output = tmp_path / "verified.jsonl"
    (tmp_path / "unsloth.log").write_text("sk-unsloth-SECRET")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["prompt.py", str(data), str(output), str(template)])
    original = ProvenanceConfig.get()
    try:
        module.main()
    finally:
        ProvenanceConfig.set(original)

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["annotation"] for row in rows] == [{"valid": True}] * 2
    prov = tmp_path / "provenance" / "verified.json"
    assert prov.exists()
    assert not prov.with_suffix(".jsonl").exists()
    document = json.loads(prov.read_text())
    acts = [a for a in document["provenance"] if a.get("type") == "prov:Activity"]
    assert len(acts) == 3
    assert "sk-unsloth-SECRET" not in prov.read_text()
    assert all("duration" in a for a in acts)
