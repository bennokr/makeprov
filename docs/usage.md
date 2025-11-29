# Usage guide

This guide walks through the typical workflow for defining rules, wiring them
into the command-line interface, and inspecting the provenance artifacts
produced by `makeprov`.

## Defining rules

Rules are simple Python callables annotated with {class}`makeprov.paths.InPath`
for dependencies and {class}`makeprov.paths.OutPath` for outputs. The
{func}`makeprov.core.rule` decorator handles dependency inference, timestamp
checks, and provenance writing.

```python
from makeprov import InPath, OutPath, rule

@rule()
def uppercase(src: InPath, dest: OutPath):
    """Convert a text file to uppercase."""
    dest.write_text(src.read_text().upper())
```

Invoke the function directly to perform the work and produce provenance
metadata in the configured output directory.

## Building dependency graphs

When you provide default values for {class}`~makeprov.paths.OutPath` parameters,
`makeprov` registers the rule as part of a build graph. You can then ask the
system to build a target and its prerequisites:

```python
from makeprov import build

# Builds the dependency chain ending at data/output.txt
build("data/output.txt")
```

Use {func}`makeprov.core.build_all` to trigger every terminal target in the
graph, which is convenient for CI pipelines.

### Parameterized targets

Default {class}`InPath` or {class}`OutPath` arguments can contain
``str.format``-style placeholders. The decorator stores the associated
templates and uses {mod}`parse` to extract parameters from requested targets:

```python
@rule()
def align(
    sample: int | None = None,
    read1: InPath = InPath("reads/{sample:d}_R1.fq"),
    bam: OutPath = OutPath("results/{sample:d}.bam"),
):
    bam.write_text(read1.read_text())

build("results/42.bam")  # calls align(sample=42)
```

### Phony/meta rules

Pass ``phony=True`` to {func}`makeprov.core.rule` to register orchestration or
reporting helpers that do not produce outputs or should always run regardless of
timestamps. These rules still participate in the CLI via {data}`makeprov.core.COMMANDS`.

## Command-line entry point

The {func}`makeprov.config.main` helper exposes decorated rules as CLI
subcommands using `defopt`. Any `--conf` options you pass are applied before the
rules run, making it easy to tailor provenance behavior per invocation.

```bash
python -m makeprov --conf @config/provenance.toml uppercase data/input.txt data/output.txt
```

Combine `--verbose` flags to increase logging during command execution, or use
``--explain`` / ``--to-dot`` to inspect dependency resolution without executing
rules:

```bash
python -m makeprov -vv uppercase data/input.txt data/output.txt
python -m makeprov --explain data/output.txt
python -m makeprov --to-dot data/output.txt
```

## Streaming input and output

All path marker classes accept the hyphen (`-`) to represent standard streams.
This makes it simple to incorporate your rules into shell pipelines without
creating temporary files:

```python
from makeprov import InPath, OutPath, rule

@rule()
def word_count(src: InPath = InPath("-"), dest: OutPath = OutPath("-")):
    """Count words from stdin and write the result to stdout."""
    content = src.read_text()
    dest.write_text(str(len(content.split())))
```
