import pytest
from pathlib import Path
from makeprov import rule, InFile, OutFile

@rule(name="test_process_data")
def process_data(input_file: InFile, output_file: OutFile):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        outfile.write(data)

def test_process_data(tmp_path):
    input_file = tmp_path / "input.txt"
    output_file = tmp_path / "output.txt"

    input_file.write_text("Hello, world!")

    # Run the process_data function
    result = process_data(InFile(str(input_file)), OutFile(str(output_file)))

    # Check that the output file was created and contains the correct data
    assert output_file.exists()
    assert output_file.read_text() == "Hello, world!"