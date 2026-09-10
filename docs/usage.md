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
subcommands using `defopt`, which is an optional dependency
(`pip install "makeprov[cli]"`). Call it from your own script's
`if __name__ == "__main__":` block — there is no `makeprov` console script,
since the set of subcommands is defined by your rules, not by the library:

```python
# my_workflow.py
from makeprov import InPath, OutPath, main, rule

@rule()
def uppercase(src: InPath, dest: OutPath):
    dest.write_text(src.read_text().upper())

if __name__ == "__main__":
    main()
```

Any `--conf` options you pass are applied before the rules run, making it
easy to tailor provenance behavior per invocation:

```bash
python my_workflow.py --conf @config/provenance.toml uppercase data/input.txt data/output.txt
```

Combine `--verbose` flags to increase logging during command execution, or use
``--explain`` / ``--to-dot`` to inspect dependency resolution without executing
rules:

```bash
python my_workflow.py -vv uppercase data/input.txt data/output.txt
python my_workflow.py --explain data/output.txt
python my_workflow.py --to-dot data/output.txt
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

## Tracking outputs within directories

Use :class:`~makeprov.paths.OutDir` when a rule produces multiple files under a
common directory. The :meth:`~makeprov.paths.OutDir.file` helper returns
:class:`~makeprov.paths.OutPath` instances rooted in that directory while
recording them for provenance collection. :class:`~makeprov.paths.InDir` offers
the same tracked-directory behavior for inputs.

```python
from makeprov import InDir, OutDir, rule

@rule()
def write_assets(bundle: OutDir = OutDir("assets/v1/")):
    readme = bundle.file("README.txt")
    logo = bundle.file("logo.txt")

    readme.write_text("asset bundle\n")
    logo.write_text("v1 logo\n")
```

When the rule finishes, the provenance record includes both `assets/v1/README.txt`
and `assets/v1/logo.txt` even though only the directory was declared as a
parameter.

## Merging provenance across nested rules

Pass ``merge=True`` to :func:`~makeprov.core.rule` to accumulate provenance from
any rules invoked within the decorated function. This produces a single
provenance document spanning the entire call tree, which is especially helpful
for orchestration functions.

```python
from makeprov import InDir, InPath, OutDir, OutPath, rule

@rule()
def render_fragment(name: str, dest: OutPath = OutPath("site/fragments/{name}.txt")):
    dest.write_text(f"fragment: {name}\n")

@rule(merge=True)
def build_site(
    sample: int,
    source_dir: InDir = InDir("content/{sample:d}/"),
    out: OutDir = OutDir("site/{sample:d}/"),
):
    index = out.file("index.html")
    report = out.file("report.md")
    logo = out.file("assets/logo.txt")

    render_fragment("logo", dest=logo)
    report.write_text(source_dir.file("main.txt").read_text())
    index.write_text("<html><body>see report.md</body></html>\n")
```

Invoking ``build("site/1/")`` runs the fragment rule, writes directory outputs,
and emits a single merged provenance dataset for the entire workflow.

## Scoped spans and explicit outputs

Use :func:`makeprov.span` to bracket arbitrary work in its own provenance
buffer. A span returns the merged :class:`~makeprov.prov.Prov` via
``span.prov`` and can write to a specific path when nested, which makes
per-model or per-shard provenance a one-liner:

```python
from makeprov import span, rule, OutPath

@rule()
def train_model(out: OutPath = OutPath("models/a.txt")):
    out.write_text("ok")

with span("model-a", prov_path="prov/models/a") as sp:
    train_model()
    assert sp.prov.name == "model-a"
```

## Controlling provenance framing

``Prov`` objects can be serialized directly via :meth:`~makeprov.prov.Prov.to_jsonld`
and :meth:`~makeprov.prov.Prov.to_graph`, which mirror the
:class:`~makeprov.rdfmixin.RDFMixin` API. By default the provenance graph is
stored in the default RDF graph, but setting ``frame = "results"`` in config moves 
it into a dedicated named graph while leaving result entities in the default graph.
In JSON-LD that produces a nested structure under ``"provenance"`` keyed by the
graph identifier, while TriG outputs add a named provenance context alongside
the default graph.
