import pathlib

import rdflib
from rdflib import Namespace
from rdflib.namespace import RDF, RDFS, XSD
from pyshacl import validate


SHAPES_FILE = pathlib.Path(__file__).with_name("prov_shapes.ttl")


def build_minimal_prov_graph() -> rdflib.Graph:
    """Build a small provenance graph matching _write_provenance_dataset."""
    g = rdflib.Graph()

    PROV = Namespace("http://www.w3.org/ns/prov#")
    DCT = Namespace("http://purl.org/dc/terms/")
    BASE = Namespace("http://example.org/")

    g.bind("prov", PROV)
    g.bind("dct", DCT)
    g.bind("", BASE)

    run_id = "20250101T000000"
    name = "example"

    activity = BASE[f"run/{name}/{run_id}"]
    agent = BASE["agent/script.py"]
    src = BASE["src/input.txt"]
    out = BASE["out/output.txt"]
    graph_ent = BASE[f"graph/{name}"]
    env = BASE[f"env/{run_id}"]
    dep = rdflib.URIRef("https://pypi.org/project/rdflib/")
    rdf_resource = rdflib.URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#Resource")

    t0 = rdflib.Literal("2025-01-01T00:00:00Z", datatype=XSD.dateTime)
    t1 = rdflib.Literal("2025-01-01T00:01:00Z", datatype=XSD.dateTime)

    # Activity
    g.add((activity, RDF.type, PROV.Activity))
    g.add((activity, PROV.startedAtTime, t0))
    g.add((activity, PROV.endedAtTime, t1))

    # Software agent
    g.add((agent, RDF.type, PROV.Agent))
    g.add((agent, RDF.type, PROV.SoftwareAgent))
    g.add((agent, RDFS.label, rdflib.Literal("script.py")))
    g.add((activity, PROV.wasAssociatedWith, agent))

    # File entities (src and out)
    for ent in (src, out):
        g.add((ent, RDF.type, PROV.Entity))
        g.add((ent, DCT["format"], rdflib.Literal("text/plain")))
        g.add((ent, DCT["extent"], rdflib.Literal(123, datatype=XSD.integer)))
        g.add((ent, DCT["modified"], t1))

    # Source file gets a sha256 identifier
    g.add((src, DCT["identifier"], rdflib.Literal("sha256:abc123")))

    # Link activity to src (used) and out (generated)
    g.add((activity, PROV.used, src))
    g.add((out, PROV.wasGeneratedBy, activity))

    # Graph entity (named data graph)
    g.add((graph_ent, RDF.type, PROV.Entity))
    g.add((graph_ent, PROV.wasGeneratedBy, activity))
    g.add((graph_ent, PROV.wasAttributedTo, agent))
    g.add((graph_ent, PROV.generatedAtTime, t1))

    # Environment entity
    g.add((env, RDF.type, PROV.Entity))
    g.add((env, RDF.type, PROV.Collection))
    g.add((env, RDFS.label, rdflib.Literal("Python environment")))
    g.add((env, DCT["title"], rdflib.Literal("mypackage")))
    g.add((env, DCT["hasVersion"], rdflib.Literal("0.1.0")))
    g.add((env, DCT["requires"], dep))
    g.add((activity, PROV.used, env))

    # Dependency (PyPI project IRI)
    g.add((dep, RDF.type, rdf_resource))
    g.add((dep, RDFS.label, rdflib.Literal("rdflib>=6.0.0")))

    return g


def test_prov_conforms_to_shapes():
    data_graph = build_minimal_prov_graph()

    shapes_graph = rdflib.Graph()
    shapes_graph.parse(str(SHAPES_FILE), format="turtle")

    conforms, _, report_text = validate(
        data_graph=data_graph,
        shacl_graph=shapes_graph,
        inference="rdfs",
        debug=False,
    )

    assert conforms, report_text
