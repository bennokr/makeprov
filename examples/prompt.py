#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "llm>=0.32",
#     "makeprov>=0.7.2,<0.8",
# ]
# ///

import argparse
import json
import re
import urllib.request
from pathlib import Path

from llm.default_plugins.openai_models import Chat
from llm.utils import extract_fenced_code_block
from makeprov import InPath, OutPath, ProvMeta, ProvenanceConfig, rule

ENDPOINT = "http://127.0.0.1:8888/v1"
UNSLOTH_LOG = "unsloth.log"
REASONING_EFFORT = "none"


def get_api_key():
    keys = re.findall(r"sk-unsloth-[A-Za-z0-9_-]+", Path(UNSLOTH_LOG).read_text())
    if not keys:
        raise RuntimeError(f"No Unsloth API key found in {UNSLOTH_LOG}")
    return keys[-1]


def get_model_id(api_key):
    req = urllib.request.Request(
        f"{ENDPOINT}/models", headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(req) as response:
        models = json.load(response).get("data", [])
    if not models:
        raise RuntimeError("Unsloth returned no models")
    return models[0]["id"]


@rule(phony=True)
def annotate(
    row: dict,
    model: Chat,
    api_key: str,
    prompt_template: str,
    fout,
    index: ProvMeta[int],
    record_id: ProvMeta[str | None],
    reasoning_effort: str = REASONING_EFFORT,
):
    prompt = prompt_template + "\n\n" + json.dumps(row, ensure_ascii=False)
    response = model.prompt(
        prompt, key=api_key, options={"reasoning_effort": reasoning_effort}
    )
    text = response.text().strip()
    extracted = extract_fenced_code_block(text)
    if extracted is not None:
        text = extracted.strip()
    try:
        row["annotation"] = json.loads(text)
    except json.JSONDecodeError:
        row["annotation"] = text

    fout.write(json.dumps(row, ensure_ascii=False) + "\n")
    fout.flush()

    message = (response.response_json.get("choices") or [{}])[0].get("message", {})
    if reasoning := message.get("reasoning_content"):
        print(f"\n[{index}] reasoning:\n{reasoning.strip()}")
    print(f"[{index}] done")


@rule(name="verify_jsonl", force=True)
def verify_jsonl(
    input_path: InPath,
    output_path: OutPath,
    prompt_path: InPath,
    model_id: ProvMeta[str],
    api_key: str,
    api_base: ProvMeta[str] = ENDPOINT,
    reasoning: ProvMeta[bool] = True,
    reasoning_effort: ProvMeta[str] = REASONING_EFFORT,
):
    print(f"Model: {model_id}\nReasoning effort: {reasoning_effort}")
    model = Chat(
        model_id=model_id, model_name=model_id, api_base=api_base, reasoning=reasoning
    )
    template = prompt_path.read_text().strip()

    with input_path.open() as fin, output_path.open("w") as fout:
        for index, line in enumerate(fin, 1):
            if line.strip():
                row = json.loads(line)
                annotate(
                    row, model, api_key, template, fout,
                    index=index, record_id=row.get("id"),
                    reasoning_effort=reasoning_effort,
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("prompt")
    args = parser.parse_args()

    output = OutPath(args.output)
    provenance_base = Path(output).parent / "provenance" / Path(output).name
    ProvenanceConfig.set(ProvenanceConfig(
        prov_path=str(provenance_base), context=True, stream=True,
    ))
    api_key = get_api_key()
    verify_jsonl(
        InPath(args.input), output, InPath(args.prompt),
        model_id=get_model_id(api_key), api_key=api_key,
    )


if __name__ == "__main__":
    main()
