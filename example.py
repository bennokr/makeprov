from makeprov import rule, InPath, OutPath, main


# Define step 1: Preprocess
@rule()
def preprocess(
    input_file: InPath = InPath("README.md"),
    output_file: OutPath = OutPath("data/intermediate.txt"),
):
    with input_file.open("r") as inpath, output_file.open("w") as outpath:
        data = inpath.read()
        # Example processing: Convert to uppercase
        outpath.write(data.upper())


# Define step 2: Process
@rule()
def process(
    input_file: InPath = OutPath("data/intermediate.txt"),
    output_file: OutPath = OutPath("data/summary.txt"),
):
    with input_file.open("r") as inpath, output_file.open("w") as outpath:
        data = inpath.read()
        # Example processing: Count words
        word_count = len(data.split())
        outpath.write(f"Word Count: {word_count}\n")


# Define step 3: Postprocess
@rule()
def postprocess(
    input_file: InPath = OutPath("data/summary.txt"),
    output_file: OutPath = OutPath("data/example.txt"),
):
    with input_file.open("r") as inpath, output_file.open("w") as outpath:
        data = inpath.readlines()
        # Example processing: Add a summary line
        summary = "Summary: Process completed.\n"
        outpath.write(summary)
        outpath.writelines(data)


if __name__ == "__main__":
    main()
