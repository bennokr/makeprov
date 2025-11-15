# `makeprov`: Provenance Tracking Made Easy

This library provides a way to track file provenance in Python workflows using RDF and PROV (W3C Provenance) semantics. It supports defining input/output files via decorators and automatically generates provenance datasets.

## Features

- Use decorators to define rules for workflows.
- Automatically generate RDF-based provenance metadata.
- Handles input and output streams.
- Integrates with Python's type hints for easy configuration.
- Outputs provenance data in TRIG format.

## Installation

You can install the module directly from PyPI:

```bash
pip install makeprov
```

## Usage

Here’s an example of how to use this package in your Python scripts:

```python
from makeprov import rule, InFile, OutFile

@rule()
def process_data(input_file: InFile, output_file: OutFile):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        outfile.write(data)

if __name__ == '__main__':
    process_data(InFile('input.txt'), OutFile('output.txt'))

    # or
    import defopt
    defopt.run(process_data)

    # or
    from makeprov import build
    build('output.txt')
```

You can execute `example.py` via the CLI like so:

```bash
# Set configuration through the CLI
python example.py --conf='{"base_iri": "http://mybaseiri.org/", "prov_dir": "my_prov_directory"}' --force --input_file input.txt --output_file final_output.txt
```

### Complex CSV-to-RDF Workflow

For a more involved scenario, see [`complex_example.py`](complex_example.py). It creates multiple CSV files, aggregates their contents, and emits an RDF graph that is both serialized to disk and embedded into the provenance dataset because the function returns an `rdflib.Graph`.

```python
@rule()
def export_totals_graph(
    totals_csv: InFile = InFile("data/region_totals.csv"),
    graph_ttl: OutFile = OutFile("data/region_totals.ttl"),
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

You can customize the provenance tracking with options like `base_iri`, `prov_dir`, and more.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.