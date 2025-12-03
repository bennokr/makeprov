# Snakemake integration

`makeprov` can read Snakemake's execution metadata without depending on
Snakemake's Python API. The optional submodule shells out to the `snakemake`
CLI, collects the job DAG (`--d3dag`) and the detailed summary table
(`--detailed-summary`), and builds a provenance graph that can be bundled into
Snakemake HTML reports.

## Installation

Install the optional extra so the Snakemake CLI is available alongside
`makeprov`:

```bash
pip install "makeprov[snakemake]"
```

## CLI entry point

The bridge is exposed as a module entry point. All arguments after `--` are
passed through to Snakemake unchanged.

```bash
python -m makeprov.snakemake \
    --prov-path prov/run \
    --out-fmt json \
    --forceall-dag \
    -- \
    --snakefile Snakefile --nolock
```

Key flags:

- `--prov-dir` / `--prov-path` mirror the configuration fields from
  `makeprov.config`.
- `--out-fmt` accepts `json` (default) or `trig`.
- `--context` embeds the JSON-LD context inline, which is useful when
  generating standalone files for reports.
- `--frame` chooses between the `provenance` (default) and `results` JSON-LD
  frames.
- `--forceall-dag` adds `--forceall` to the Snakemake `--d3dag` invocation so
  edges between already up-to-date jobs remain part of the provenance graph.

The command honours `-c/--conf` arguments in the same way as the core CLI,
allowing TOML configuration snippets or files to override defaults.

## Example Snakefile

```python
report: "report/workflow.rst"

rule all:
    input:
        report("results/word_count.txt", category="Results"),
        report("prov/snakemake.json", category="Provenance")

rule concat:
    input:
        "data/a.txt",
        "data/b.txt"
    output:
        "results/concatenated.txt"
    shell:
        "cat {input} > {output}"

rule count_words:
    input:
        "results/concatenated.txt"
    output:
        "results/word_count.txt"
    shell:
        "wc -w {input} > {output}"

rule provenance:
    input:
        "results/word_count.txt"
    output:
        "prov/snakemake.json"
    shell:
        (
            "python -m makeprov.snakemake "
            "--prov-path prov/snakemake "
            "--out-fmt json --context --forceall-dag "
            "-- "
            "--snakefile {workflow.snakefile} --nolock {input} "
        )
```

Running `snakemake --cores 1 --report report.html` now bundles the PROV file
into the generated HTML report under the "Provenance" category.

## Output structure

The command produces a single `makeprov.prov.Prov` document. Each job becomes a
`prov:Activity`; inputs and outputs are represented as `prov:Entity` nodes, and
job-to-job edges are recorded using `prov:wasInformedBy` whenever Snakemake's
D3 DAG provides the necessary IDs. File metadata such as hashes, MIME types and
timestamps are captured from the filesystem when available.

Use the standard `makeprov` serialization helpers to post-process the output:

```python
from makeprov import snakemake

prov = snakemake.build_prov_from_snakemake(dag_json, summary_rows, config=config)
prov.write("prov/snakemake", fmt="trig")
```
