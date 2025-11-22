# Configuration

`makeprov` reads settings from :data:`makeprov.config.GLOBAL_CONFIG` and allows
runtime overrides via the command line. This page summarizes the available
options and demonstrates common configurations.

## Available options

| Field      | Description |
|------------|-------------|
| `base_iri` | Default IRI used to construct provenance identifiers. |
| `prov_dir` | Directory where provenance documents are written when no explicit path is set. |
| `prov_path` | Explicit path to the provenance document; overrides `prov_dir`. |
| `force` | When true, run rules regardless of timestamp checks. |
| `merge` | When true, merge provenance from multiple rules into a single document. |
| `dry_run` | Log actions without running rule bodies. |
| `out_fmt` | Output format: `"json"` for JSON-LD or `"trig"` for RDF TriG. |
| `context` | Embed JSON-LD context in output documents. |

## Applying overrides

Supply one or more ``--conf`` flags to :func:`makeprov.config.main`. Each flag
accepts either a TOML snippet or an ``@``-prefixed path to a TOML file.

```bash
python -m makeprov --conf '{prov_dir="artifacts/prov"}' my_rule
python -m makeprov --conf @config/provenance.toml --conf '{out_fmt="trig"}' my_rule
```

You can also update the configuration programmatically:

```python
from makeprov.config import GLOBAL_CONFIG, apply_config

apply_config(GLOBAL_CONFIG, '{force=true, out_fmt="trig"}')
```

## Example TOML file

```toml
base_iri = "https://example.org/makeprov"
prov_dir = "build/prov"
out_fmt = "trig"
merge = true
```
