from makeprov import rule, InFile, OutFile, GLOBAL_CONFIG
from config import cli, main

# Define step 1: Preprocess
@rule()
@cli
def preprocess(input_file: InFile, output_file: OutFile):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        # Example processing: Convert to uppercase
        outfile.write(data.upper())

# Define step 2: Process
@rule()
@cli
def process(input_file: InFile, output_file: OutFile):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        # Example processing: Count words
        word_count = len(data.split())
        outfile.write(f"Word Count: {word_count}\n")

# Define step 3: Postprocess
@rule()
@cli
def postprocess(input_file: InFile, output_file: OutFile):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.readlines()
        # Example processing: Add a summary line
        summary = "Summary: Process completed.\n"
        outfile.write(summary)
        outfile.writelines(data)

# Define the build-all command
@cli
def build_all(input_file: InFile, output_file: OutFile):
    # Run all steps in sequence with inferred provenance
    preprocess(input_file, OutFile('intermediate.txt'))
    process(InFile('intermediate.txt'), OutFile('summary.txt'))
    postprocess(InFile('summary.txt'), output_file)

if __name__ == '__main__':
    main(GLOBAL_CONFIG)
