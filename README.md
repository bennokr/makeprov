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

### Configuration

You can customize the provenance tracking with options like `base_iri`, `prov_dir`, and more.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.