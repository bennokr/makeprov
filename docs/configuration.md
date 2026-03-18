# Configuration

`makeprov` reads settings from a process-wide configuration instance stored on
{class}`makeprov.ProvenanceConfig` and allows runtime overrides via the command
line. This page summarizes the available options and demonstrates common
configurations.

## Available options

| Field      | Description |
|------------|-------------|
| `base_iri` | Default IRI used to construct provenance identifiers. |
| `prov_dir` | Directory where provenance documents are written when no explicit path is set. |
| `prov_path` | Explicit path to the provenance document; overrides `prov_dir`. |
| `force` | When true, run rules regardless of timestamp checks. |
| `merge` | When true, collect provenance in a workflow-level buffer and emit a single document at the end of the run. |
| `dry_run` | Log actions without running rule bodies. |
| `out_fmt` | Output format: `"json"` for JSON-LD or `"trig"` for RDF TriG. |
| `context` | Embed JSON-LD context in output documents. |
| `context_url` | URL to reference in `@context` when `context` is `false`. |

## Applying overrides

Supply one or more ``--conf`` flags to {func}`makeprov.config.main`. Each flag
accepts either a TOML snippet or an ``@``-prefixed path to a TOML file.

```bash
python -m makeprov --conf '{prov_dir="artifacts/prov"}' my_rule
python -m makeprov --conf @config/provenance.toml --conf '{out_fmt="trig"}' my_rule
```

You can also update the configuration programmatically:

```python
from makeprov import ProvenanceConfig

cfg = ProvenanceConfig.get()
cfg.apply('{force=true, out_fmt="trig"}')
ProvenanceConfig.set(cfg.clone_with(prov_dir="artifacts/prov"))
```

## Merge semantics

When ``merge`` is true (the default), ``makeprov`` opens a single provenance
buffer for the workflow run and appends provenance from every rule to that
buffer. The buffer is flushed once at the end of the run (by the CLI entrypoint
or the top-level call to {func}`makeprov.core.build`), ensuring that nested
rules never emit their own documents. Any per-rule ``prov_path`` is ignored
while a workflow buffer is active; set ``merge=false`` to force a rule to write
its own provenance file immediately.

## Context isolation

The JSON-LD context in ``makeprov`` is treated as an immutable template. Each
call to {class}`makeprov.prov.Prov.create` copies the common context and applies
repository-specific defaults (such as ``@base`` and ``blob`` IRIs discovered
from git) to the copy instead of mutating shared globals. This guarantees that
one workflow run cannot leak context updates into another. Use
{class}`makeprov.ProvenanceConfig` to set a ``base_iri`` for identifiers without
worrying about cross-run side effects.

## Isolating state with sessions

Rule registries and provenance buffers live in a :class:`~makeprov.core.Session`
object. ``makeprov`` creates a default session for convenience, but you can
instantiate your own to keep multiple runs from sharing state:

```python
from makeprov import OutPath, build, new_session, rule

session = new_session()

@rule(session=session)
def generate(out: OutPath = OutPath("data/output.txt")):
    out.write_text("content")

build("data/output.txt", session=session)
```

Pass the same ``session`` to CLI entrypoints via
{func}`makeprov.config.main` to ensure commands and buffers remain isolated.

## Context handling

Set `context=true` to embed the JSON-LD context inline from `src/makeprov/context.jsonld`.  
When `context=false`, the `@context` field is set to `context_url` (default: `https://w3id.org/makeprov/context`), so you can pin consumers to a specific published version without hard-coding it in code. Override `context_url` in a TOML snippet or file if you host the context elsewhere.

## Inspecting dependency graphs

Two CLI flags help you explore build order without executing any rules:

- ``--explain TARGET`` logs which rule will satisfy a target.
- ``--to-dot TARGET`` prints a Graphviz DOT representation of the dependency graph.

## Example TOML file

```toml
base_iri = "https://example.org/makeprov"
prov_dir = "build/prov"
out_fmt = "trig"
merge = true
```
