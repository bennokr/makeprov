# Provenance output

Every rule decorated with :func:`makeprov.core.rule` can emit provenance records
that describe the activity, inputs, outputs, environment, and results. The
library supports JSON-LD and TriG outputs, aligning with the W3C PROV data
model.

## JSON-LD structure

When ``out_fmt`` is set to ``"json"``, the resulting file contains a top-level
``provenance`` array with entries for activities, agents, entities, and optional
result graphs. Set ``context=True`` to embed the JSON-LD context alongside the
graph data.

```json
{
  "@context": { "prov": "http://www.w3.org/ns/prov#" },
  "provenance": [
    {"@id": "script.py#uppercase-20240101T120000", "@type": "prov:Activity"}
  ]
}
```

## TriG datasets

TriG output writes a dataset that combines the default graph for provenance with
named graphs for any :class:`makeprov.rdfmixin.RDFMixin` results returned by a
rule. This is useful when merging provenance with domain-specific RDF data.

```python
output = prov.write("prov/uppercase", fmt="trig")
print(output)
```

## Environment capture

If the calling project is installed as a Python distribution, the library
captures the package name, version, and dependencies as part of the provenance.
This information is attached as a collection entity linked to the generating
activity.

## Result graphs

Return instances of :class:`~makeprov.rdfmixin.RDFMixin` from your rules to
include domain-specific RDF alongside provenance. Each result object is embedded
in a named graph so consumers can query both provenance and outputs together.
