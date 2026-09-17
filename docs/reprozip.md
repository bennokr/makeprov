# ReproZip graph conversion

Convert an **existing** ReproZip graph to makeprov's PROV/JSON-LD or RDF/TriG representation. The converter has no ReproZip runtime dependency and does not trace or run commands itself.

```bash
reprozip trace -- your-command arg1 arg2
reprounzip graph --json graph.json
makeprov-reprozip graph.json --output prov/command --base-iri https://example.org/my-experiment/
# Writes prov/command.json, with the JSON-LD context embedded.
```

Alternatively, use `python -m makeprov.reprozip graph.json`. For RDF/TriG, install `makeprov[rdf]` and add `--format trig`.

The Python interface is `convert_reprozip_graph(path, config=ProvenanceConfig(...))` or `build_prov_from_reprozip(graph_dict, config=...)`, both returning a `makeprov.prov.Prov` object.

The input is the JSON file produced by `reprounzip graph --json`, **not** `.rpz`, `trace.sqlite3`, the DOT graph, or ReproZip's `config.yml`.

## Interpretation and limits

- Process entries become `prov:Activity`; observed file reads become `prov:used`, and observed writes become generated file entities. These are **observed accesses**, not declared semantic dependencies or proof that an input influenced an output.
- Parent process indices are local to each run. Parent relationships are represented with `prov:wasInformedBy`, retaining the `exec`/`fork` reason as metadata.
- Raw numeric start times are preserved as metadata, not converted into `prov:startedAtTime`: the graph JSON does not declare their clock or units. ISO 8601 timestamps, if supplied, are converted to `prov:startedAtTime`.
- If multiple activities write the same pathname, or one activity both reads and writes it, distinct output entities avoid attributing a single entity to multiple generation events. The graph cannot establish which file version a later read consumed.
- Binary/package information is retained, when present. File contents, hashes, end times and network traffic are **not** provided by this graph format. ReproZip may omit or aggregate file accesses depending on the options used when generating the graph.
- Absolute paths become `file:` IRIs. Use `--base-iri` to give the trace and process nodes an absolute namespace. The converter embeds its context by default so its added relationship terms expand correctly.
