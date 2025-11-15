from makeprov import rule, InFile, OutFile, GLOBAL_CONFIG, COMMANDS
from config import main

# Define step 1: Preprocess
@rule()
def preprocess(input_file: InFile, output_file: OutFile):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        # Example processing: Convert to uppercase
        outfile.write(data.upper())

# Define step 2: Process
@rule()
def process(input_file: InFile, output_file: OutFile):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        # Example processing: Count words
        word_count = len(data.split())
        outfile.write(f"Word Count: {word_count}\n")

# Define step 3: Postprocess
@rule()
def postprocess(input_file: InFile, output_file: OutFile):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.readlines()
        # Example processing: Add a summary line
        summary = "Summary: Process completed.\n"
        outfile.write(summary)
        outfile.writelines(data)

# Define the build-all command
@rule()
def build_all(input_file: InFile=InFile('README.md'), output_file: OutFile=OutFile('data/example.txt')):
    # Run all steps in sequence with inferred provenance
    intermediate = OutFile('data/intermediate.txt')
    summary = OutFile('data/summary.txt')
    preprocess(input_file, intermediate)
    process(intermediate.as_infile(), summary)
    postprocess(summary.as_infile(), output_file)

if __name__ == '__main__':
    main(COMMANDS, GLOBAL_CONFIG)
