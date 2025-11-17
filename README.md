# makeprov: Pythonic Provenance Tracking

This library provides a way to track file provenance in Python workflows using PROV (W3C Provenance) semantics. It supports defining input/output files via decorators and automatically generates provenance datasets.

## Features

- Use decorators to define rules for workflows.
- Automatically generate RDF-based provenance metadata.
- Handles input and output streams.
- Integrates with Python's type hints for easy configuration.
- Outputs provenance data in TRIG format if `rdflib` is installed; otherwise outputs json-ld.

## Installation

You can install the module directly from PyPI:

```bash
pip install makeprov
```

## Usage

Here’s an example of how to use this package in your Python scripts:

```python
from makeprov import rule, InPath, OutPath, build

@rule()
def process_data(
    input_file: InPath = InPath('input.txt'), 
    output_file: OutPath = OutPath('output.txt')
):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        outfile.write(data.upper())

if __name__ == '__main__':
    process_data()

    # or as a command line interface
    import defopt
    defopt.run(process_data)

    # or as a workflow graph that automatically (re)generates all dependencies
    from makeprov import build
    build('output.txt')
```

You can execute `example.py` via the CLI like so:

```bash
python example.py build-all

# Or set configuration through the CLI
python example.py build-all --conf='{"base_iri": "http://mybaseiri.org/", "prov_dir": "my_prov_directory"}' --force --input_file input.txt --output_file final_output.txt

# Or set configuration through a TOML file
python example.py build-all --conf=@my_config.toml
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