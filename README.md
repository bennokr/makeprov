# makeprov: Pythonic Provenance Tracking

This library provides a way to track file provenance in Python workflows using PROV (W3C Provenance) semantics. Decorators declare inputs and outputs, provenance is written automatically, and templated targets can be resolved on demand.

## Features

- Use decorators to define rules for workflows.
- Resolve templated targets (``results/{sample}.txt``) via ``parse``-style patterns.
- Support phony/meta rules for orchestration alongside file-producing rules.
- Automatically generate RDF-based provenance metadata (`rdflib` optional).
- Handles input and output streams.
- Integrates with Python's type hints for easy configuration.
- Outputs provenance data in TRIG format if `rdflib` is installed; otherwise outputs json-ld.
- Optional Snakemake CLI integration that turns `--d3dag` and `--detailed-summary`
  output into PROV JSON-LD artifacts ready for inclusion in Snakemake HTML reports.

## Installation

You can install the module directly from PyPI:

```bash
pip install makeprov
```

Install the Snakemake extra if you want to use the CLI bridge:

```bash
pip install "makeprov[snakemake]"
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

You can execute `example.py` via the CLI like so:

```bash
python example.py build-all

# Or set configuration through the CLI
python example.py build-all --conf='{"base_iri": "http://mybaseiri.org/", "prov_dir": "my_prov_directory"}' --force --input_file input.txt --output_file final_output.txt

# Or set configuration through a TOML file
python example.py build-all -c @my_config.toml

# Inspect dependency resolution without executing rules
python example.py --explain results/1.txt
python example.py --to-dot results/1.txt
```

### Complex CSV-to-RDF Workflow

For a more involved scenario, see [`complex_example.py`](complex_example.py). It creates multiple CSV files, aggregates their contents, and emits an RDF graph that is both serialized to disk and embedded into the provenance dataset because the function returns an `rdflib.Graph`.

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
python complex_example.py build-sales-report
```

### Bundling nested provenance and directory outputs

Rules can merge the provenance from any rules they invoke by passing
``merge=True`` to `makeprov.rule`. Pair this with
`makeprov.OutDir` to declare a directory and then materialize multiple
outputs beneath it while keeping them linked to a single provenance record. Use
`makeprov.InDir` for the same tracked-directory semantics on inputs.
See [`merge_outdir_example.py`](merge_outdir_example.py) for an example.

### Snakemake workflows

`makeprov` ships with an optional subcommand that shells out to Snakemake and
converts the job DAG together with ``--detailed-summary`` metadata into a PROV
document. The CLI mirrors the familiar configuration flags from
`makeprov.config` and writes JSON-LD by default.

```bash
python -m makeprov.snakemake --prov-path prov/snakemake -- --snakefile Snakefile --nolock
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
            "python -m makeprov.snakemake "
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

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
