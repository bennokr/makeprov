import json
import sys
import tempfile
from pathlib import Path

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from makeprov import InPath, OutPath, ProvenanceConfig, build, main, rule

@rule(name="test_process_data")
def process_data(input_file: InPath, output_file: OutPath):
    with input_file.open('r') as infile, output_file.open('w') as outfile:
        data = infile.read()
        outfile.write(data)


SALES_NS = Namespace("http://example.org/test/")
TEST_PROV_DIR = Path(tempfile.mkdtemp(prefix="makeprov-tests-"))
TEST_PROV_CONFIG = ProvenanceConfig(prov_dir=str(TEST_PROV_DIR))


@rule(name="test_totals_graph", config=TEST_PROV_CONFIG)
def totals_graph(input_csv: InPath, graph_out: OutPath) -> Graph:
    graph = Graph()
    graph.bind("sales", SALES_NS)


    with input_csv.open('r') as handle:
        for line in handle.read().strip().splitlines()[1:]:
            region, units, revenue = line.split(',')
            subject = SALES_NS[f"region/{region.lower()}"]
            graph.add((subject, RDF.type, SALES_NS.RegionTotal))
            graph.add((subject, SALES_NS.regionName, Literal(region)))
            graph.add((subject, SALES_NS.totalUnits, Literal(units, datatype=XSD.integer)))
            graph.add((subject, SALES_NS.totalRevenue, Literal(revenue, datatype=XSD.decimal)))

    with graph_out.open('w') as handle:
        handle.write(graph.serialize(format='turtle'))

    return graph


def test_process_data(tmp_path):
    input_file = tmp_path / "input.txt"
    output_file = tmp_path / "output.txt"

    input_file.write_text("Hello, world!")

    # Run the process_data function
    result = process_data(InPath(str(input_file)), OutPath(str(output_file)))

    # Check that the output file was created and contains the correct data
    assert output_file.exists()
    assert output_file.read_text() == "Hello, world!"


def test_rule_returns_graph(tmp_path):
    input_csv = tmp_path / "region_totals.csv"
    graph_ttl = tmp_path / "region_totals.ttl"
    input_csv.write_text("region,total_units,total_revenue\nNorth,6,119.94\n")

    result = totals_graph(InPath(str(input_csv)), OutPath(str(graph_ttl)))

    assert isinstance(result, Graph)
    assert graph_ttl.exists()
    assert "North" in graph_ttl.read_text()
    print(*TEST_PROV_DIR.glob('*'))
    assert list(TEST_PROV_DIR.glob('*'))


def test_build_combines_provenance(tmp_path, monkeypatch):
    prov_dir = tmp_path / "prov"
    config = ProvenanceConfig(prov_dir=str(prov_dir))

    @rule(name="combine_step_one", config=config)
    def step_one(
        source: InPath = InPath("combine-source.txt"),
        mid: OutPath = OutPath("combine-mid.txt"),
    ):
        with source.open("r") as src, mid.open("w") as dst:
            dst.write(src.read() + " step1")

    @rule(name="combine_step_two", config=config)
    def step_two(
        mid: InPath = InPath("combine-mid.txt"),
        final: OutPath = OutPath("combine-final.txt"),
    ):
        with mid.open("r") as src, final.open("w") as dst:
            dst.write(src.read() + " step2")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "combine-source.txt").write_text("data")

    build("combine-final.txt")

    final_output = tmp_path / "combine-final.txt"
    assert final_output.exists()
    assert final_output.read_text() == "data step1 step2"

    prov_files = list(prov_dir.glob("*"))
    assert len(prov_files) == 1

    prov_json = json.loads(prov_files[0].read_text())
    activities = [
        node
        for node in prov_json["provenance"]
        if node.get("type") == "prov:Activity"
        or (
            isinstance(node.get("type"), list)
            and "prov:Activity" in node.get("type", [])
        )
    ]

    assert len(activities) == 2


def test_cli_merge_prov(tmp_path, monkeypatch):
    prov_dir = tmp_path / "prov"
    intermediate = tmp_path / "cli-mid.txt"
    final = tmp_path / "cli-final.txt"
    config = ProvenanceConfig(prov_dir=str(prov_dir))

    @rule(name="cli_merge_one", config=config)
    def step_one(mid: OutPath = OutPath(intermediate)):
        with mid.open("w") as dst:
            dst.write("stage1")

    @rule(name="cli_merge_two", config=config)
    def step_two(mid: InPath = InPath(intermediate), final: OutPath = OutPath(final)):
        with mid.open("r") as src, final.open("w") as dst:
            dst.write(src.read() + " stage2")

    def run_pipeline():
        step_one()
        step_two()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["prog", "--merge-prov", "run-pipeline"])

    main(subcommands=[run_pipeline])

    assert final.exists()
    assert final.read_text() == "stage1 stage2"

    prov_files = list(prov_dir.glob("*"))
    assert len(prov_files) == 1

    prov_json = json.loads(prov_files[0].read_text())
    activities = [
        node
        for node in prov_json["provenance"]
        if node.get("type") == "prov:Activity"
        or (
            isinstance(node.get("type"), list)
            and "prov:Activity" in node.get("type", [])
        )
    ]

    assert len(activities) == 2
