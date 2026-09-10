# makeprov: Pythonic Provenance Tracking

`makeprov` is a small library for recording W3C PROV/JSON-LD provenance
around Python functions that read and write files: which inputs produced
which outputs, when, with what code and environment. A decorator wraps a
function, tracks the files it declares as inputs/outputs, and writes a
provenance record after each call. A minimal `make`-style dependency
resolver and an optional Snakemake bridge are included, but the core
contract of the library is the provenance record — not workflow
orchestration, which tools like Snakemake already do well.

## Features

- Decorator-based rules that infer dependencies from `InPath`/`OutPath`
  parameters and write a PROV/JSON-LD record after every call.
- Provenance write failures are fatal by default (`ProvenanceConfig(strict=True)`),
  so a rule can't silently "succeed" with no record of what it did.
- Resolve templated targets (``results/{sample}.txt``) via ``parse``-style patterns,
  and a small dependency resolver (`build`/`build_all`) for chaining rules.
- Serialize provenance as JSON-LD, or as RDF/TriG when `rdflib` is installed
  (`pip install "makeprov[rdf]"`).
- Optional Snakemake bridge that turns `--d3dag` and `--detailed-summary`
  output into PROV JSON-LD artifacts ready for inclusion in Snakemake HTML reports.

## Installation

You can install the module directly from PyPI:

```bash
pip install makeprov
```

Optional extras add RDF/TriG export, CLI subcommand support, or the Snakemake bridge:

```bash
pip install "makeprov[rdf]"        # rdflib + pyshacl for RDF/TriG export
pip install "makeprov[cli]"        # defopt, needed for makeprov.main()
pip install "makeprov[snakemake]"  # the makeprov-snakemake bridge
```

## Usage

Here’s an example of how to use this package in your Python scripts:

```python
from makeprov import rule, InPath, OutPath, build

@rule()
def process_data(
    sample: int | None = None,
    input_file: InPath = InPath('data/{sample:d}.txt'),
    output_file: OutPath = OutPath('results/{sample:d}.txt')
):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        outfile.write(data.upper())

if __name__ == '__main__':
    # Build a specific templated target and its prerequisites
    from makeprov import build
    build('results/1.txt')

    # Or expose rules via a command line interface
    import defopt
    defopt.run(process_data)
```

You can execute `examples/example.py` via the CLI like so:

```bash
python examples/example.py build-all

# Or set configuration through the CLI
python examples/example.py build-all --conf='{"base_iri": "http://mybaseiri.org/", "prov_dir": "my_prov_directory"}' --force --input_file input.txt --output_file final_output.txt

# Or set configuration through a TOML file
python examples/example.py build-all -c @my_config.toml

# Inspect dependency resolution without executing rules
python examples/example.py --explain results/1.txt
python examples/example.py --to-dot results/1.txt
```

### Complex CSV-to-RDF Workflow

For a more involved scenario, see [`examples/complex_example.py`](examples/complex_example.py). It creates multiple CSV files, aggregates their contents, and emits an RDF graph that is both serialized to disk and embedded into the provenance dataset because the function returns an `rdflib.Graph`.

```python
@rule()
def export_totals_graph(
    totals_csv: InPath = InPath("data/region_totals.csv"),
    graph_ttl: OutPath = OutPath("data/region_totals.ttl"),
) -> Graph:
    graph = Graph()
    graph.bind("sales", SALES)

    with totals_csv.open("r", newline="") as handle:
        for row in csv.DictReader(handle):
            region_key = row["region"].lower().replace(" ", "-")
            subject = SALES[f"region/{region_key}"]

            graph.add((subject, RDF.type, SALES.RegionTotal))
            graph.add((subject, SALES.regionName, Literal(row["region"])))
            graph.add((subject, SALES.totalUnits, Literal(row["total_units"], datatype=XSD.integer)))
            graph.add((subject, SALES.totalRevenue, Literal(row["total_revenue"], datatype=XSD.decimal)))

    with graph_ttl.open("w") as handle:
        handle.write(graph.serialize(format="turtle"))

    return graph
```

Run the entire workflow, including CSV generation and RDF export, with:

```bash
python examples/complex_example.py build-sales-report
```

### Bundling nested provenance and directory outputs

Rules can merge the provenance from any rules they invoke by passing
``merge=True`` to `makeprov.rule`. Pair this with
`makeprov.OutDir` to declare a directory and then materialize multiple
outputs beneath it while keeping them linked to a single provenance record. Use
`makeprov.InDir` for the same tracked-directory semantics on inputs.
See [`examples/merge_outdir_example.py`](examples/merge_outdir_example.py) for an example.

Merging is enabled by default: top-level runs start a provenance buffer and
flush it once the CLI finishes, so downstream rules end up in one document
unless you explicitly turn buffering off with `merge=False` on a rule or in the
global config. Nested merges append to their parent buffer rather than writing
multiple files.

### Configured context and isolated sessions

`examples/context_demo_example.py` demonstrates pinning a base IRI, writing
provenance to a dedicated directory, and running rules inside an isolated
session so registries and buffers do not leak across runs:

```bash
python examples/context_demo_example.py build-all
```

### Snakemake workflows

Install the `snakemake` extra (`pip install "makeprov[snakemake]"`) to get the
`makeprov-snakemake` command, which shells out to Snakemake and converts the
job DAG together with ``--detailed-summary`` metadata into a PROV document.
It mirrors the familiar configuration flags from `makeprov.config` and writes
JSON-LD by default. Note this is a best-effort bridge: it parses Snakemake's
human-oriented text output, so treat it as a convenience for reports rather
than an authoritative source of truth — it will raise rather than guess when
it can't unambiguously parse a filename (e.g. one containing whitespace).

```bash
makeprov-snakemake --prov-path prov/snakemake -- --snakefile Snakefile --nolock
```

Wire the resulting file into a report by marking it with Snakemake’s
`report()` helper:

```python
rule provenance:
    input:
        "results/word_count.txt"
    output:
        "prov/snakemake.json"
    shell:
        (
            "makeprov-snakemake "
            "--prov-path prov/snakemake "
            "--out-fmt json --context --frame provenance "
            "-- "
            "--snakefile {workflow.snakefile} --nolock {input}"
        )
```

Using the optional `--forceall-dag` flag ensures that the job-level dependency
edges in the provenance graph remain complete even when Snakemake skips nodes
that are already up to date.

### Configuration

You can customize the provenance tracking with the following options:

 - `base_iri` (str): Base IRI for new resources
 - `prov_dir` (str): Directory for writing PROV `.json-ld` or `.trig` files
 - `force` (bool): Force running of dependencies
 - `dry_run` (bool): Only check workflow, don't run anything
 - `strict` (bool, default `True`): Raise `makeprov.ProvenanceWriteError` if a
   rule's provenance record fails to write, instead of only logging a
   warning. A rule that produces a result but no provenance record is
   treated as a failure by default; set `strict=False` to opt out per-rule
   or globally.

### Scoped spans and cached downloads

Use `makeprov.span(label, prov_path=None, frame=None, context=None)` as a
context manager or decorator to bracket a chunk of work in its own provenance
buffer. A span returns the merged `Prov` via `span.prov`, so nested spans can
emit labeled artifacts without manual slicing/merging:

```python
from makeprov import span

with span("model-run", prov_path="prov/models/model1"):
    run_model()
```

For remote resources that are cached locally, wrap the path with
`CachedDownload`. It will lazily fetch on first access and record the source
URL (and optional headers) in the provenance:

```python
from makeprov import CachedDownload, rule

@rule()
def fetch_data(meta_json=CachedDownload("https://example.org/meta.json", "cache/meta.json")):
    with meta_json.open() as handle:
        return handle.read()
```

## Documentation

Build the Sphinx docs (including autosummary API stubs) with the docs extra so
that the CLI dependencies needed for imports are available:

```bash
pip install -e ".[docs]"
python docs/build.py
```

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
