import json
import sys
import tempfile
from pathlib import Path

from rdflib import Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from makeprov import (
    InDir,
    InPath,
    OutDir,
    OutPath,
    ProvenanceConfig,
    build,
    list_targets,
    main,
    plan,
    rule,
)
from makeprov.prov import Prov


@rule(name="test_process_data")
def process_data(input_file: InPath, output_file: OutPath):
    with input_file.open("r") as infile, output_file.open("w") as outfile:
        data = infile.read()
        outfile.write(data)


SALES_NS = Namespace("http://example.org/test/")
TEST_PROV_DIR = Path(tempfile.mkdtemp(prefix="makeprov-tests-"))
TEST_PROV_CONFIG = ProvenanceConfig(prov_dir=str(TEST_PROV_DIR))


@rule(name="test_totals_graph", config=TEST_PROV_CONFIG)
def totals_graph(input_csv: InPath, graph_out: OutPath) -> Graph:
    graph = Graph()
    graph.bind("sales", SALES_NS)

    with input_csv.open("r") as handle:
        for line in handle.read().strip().splitlines()[1:]:
            region, units, revenue = line.split(",")
            subject = SALES_NS[f"region/{region.lower()}"]
            graph.add((subject, RDF.type, SALES_NS.RegionTotal))
            graph.add((subject, SALES_NS.regionName, Literal(region)))
            graph.add(
                (subject, SALES_NS.totalUnits, Literal(units, datatype=XSD.integer))
            )
            graph.add(
                (subject, SALES_NS.totalRevenue, Literal(revenue, datatype=XSD.decimal))
            )

    with graph_out.open("w") as handle:
        handle.write(graph.serialize(format="turtle"))

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
    print(*TEST_PROV_DIR.glob("*"))
    assert list(TEST_PROV_DIR.glob("*"))


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
    monkeypatch.setattr(sys, "argv", ["prog", "-c", "merge=true", "run-pipeline"])

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


def test_pattern_rules_resolve_and_format(monkeypatch, tmp_path):
    @rule(name="templated_upper")
    def templated(
        sample: int | None = None,
        src: InPath = InPath("raw/{sample:d}.txt"),
        dst: OutPath = OutPath("out/{sample:d}.txt"),
    ):
        with src.open("r") as handle, dst.open("w") as out_handle:
            out_handle.write(handle.read().upper())

    monkeypatch.chdir(tmp_path)
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "3.txt").write_text("abc")

    build("out/3.txt")

    assert (tmp_path / "out" / "3.txt").exists()
    assert (tmp_path / "out" / "3.txt").read_text() == "ABC"
    targets = list_targets()
    assert "out/3.txt" not in targets  # templated targets stay pattern based


def test_multi_output_rules_register_all_targets(monkeypatch, tmp_path):
    @rule(name="dual_outputs")
    def dual(a: OutPath = OutPath("a.txt"), b: OutPath = OutPath("b.txt")):
        a.write_text("A")
        b.write_text("B")

    monkeypatch.chdir(tmp_path)

    build("b.txt")

    assert (tmp_path / "a.txt").read_text() == "A"
    assert (tmp_path / "b.txt").read_text() == "B"
    assert set(list_targets()) >= {"a.txt", "b.txt"}


def test_phony_rules_and_plan(monkeypatch, tmp_path):
    calls: list[str] = []

    @rule(name="artifact_rule")
    def artifact(out: OutPath = OutPath("artifact.txt")):
        out.write_text("payload")

    @rule(name="report", phony=True)
    def report(_: int | None = None):
        calls.append("report")

    monkeypatch.chdir(tmp_path)

    build("artifact.txt")
    report()

    assert calls == ["report"]
    planned = plan("artifact.txt")
    assert planned[-1][1].name == "artifact_rule"


def test_cli_graph_flags(monkeypatch, capsys, tmp_path):
    @rule(name="graph_rule")
    def graph_rule(out: OutPath = OutPath("graph-target.txt")):
        out.write_text("dot")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["prog", "--to-dot", "graph-target.txt"], raising=False
    )

    main()
    stdout = capsys.readouterr().out
    assert "graph-target.txt" in stdout


def test_rule_local_merge(monkeypatch, tmp_path):
    prov_dir = tmp_path / "prov"
    config = ProvenanceConfig(prov_dir=str(prov_dir), merge=False)

    @rule(name="child", config=config)
    def child(out: OutPath = OutPath("child.txt")):
        out.write_text("child")

    @rule(name="parent", config=config, merge=True)
    def parent(out: OutPath = OutPath("parent.txt")):
        child()
        out.write_text("parent")

    monkeypatch.chdir(tmp_path)
    parent()

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


def test_outdir_tracks_outputs(monkeypatch, tmp_path):
    prov_dir = tmp_path / "prov"
    config = ProvenanceConfig(prov_dir=str(prov_dir))

    @rule(name="build_site", config=config)
    def build_site(
        sample: int,
        input_file: InPath = InPath("data/{sample:d}.txt"),
        out: OutDir = OutDir("results/{sample:d}/"),
    ):
        report = out.file("report.md")
        index = out.file("index.html")
        plot = out.file("plot.png")

        report.write_text("...")
        index.write_text("...")
        with plot.open("wb") as f:
            f.write(b"...")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "1.txt").write_text("input")

    build_site(1)

    assert (tmp_path / "results/1/report.md").exists()
    assert (tmp_path / "results/1/index.html").exists()
    assert (tmp_path / "results/1/plot.png").exists()

    prov_files = list(prov_dir.glob("*"))
    assert len(prov_files) == 1

    prov_json = json.loads(prov_files[0].read_text())
    outputs = [
        node
        for node in prov_json["provenance"]
        if node.get("wasGeneratedBy")
    ]

    assert any(node["id"].endswith("results/1/report.md") for node in outputs)
    assert any(node["id"].endswith("results/1/index.html") for node in outputs)
    assert any(node["id"].endswith("results/1/plot.png") for node in outputs)


def test_indir_tracks_inputs(monkeypatch, tmp_path):
    prov_dir = tmp_path / "prov"
    config = ProvenanceConfig(prov_dir=str(prov_dir))

    @rule(name="consume", config=config)
    def consume(
        bundle: InDir = InDir("data/{sample}/"),
        out: OutPath = OutPath("out.txt"),
        sample: str = "s1",
    ):
        main_txt = bundle.file("main.txt")
        aux_txt = bundle.file("aux/info.txt")
        content = main_txt.read_text().strip() + aux_txt.read_text().strip()
        out.write_text(content)
        return content

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "s1" / "aux").mkdir(parents=True)
    (tmp_path / "data" / "s1" / "main.txt").write_text("hello\n")
    (tmp_path / "data" / "s1" / "aux" / "info.txt").write_text("world\n")

    assert consume() == "helloworld"

    prov_files = list(prov_dir.glob("*"))
    assert len(prov_files) == 1

    prov_json = json.loads(prov_files[0].read_text())
    inputs: list[dict] = []
    for node in prov_json["provenance"]:
        used = node.get("used")
        if not used:
            continue
        for entry in used:
            if isinstance(entry, dict) and entry.get("type") == "prov:Entity":
                inputs.append(entry)

    assert any(node["id"].endswith("data/s1/main.txt") for node in inputs)
    assert any(node["id"].endswith("data/s1/aux/info.txt") for node in inputs)


def test_prov_results_frame_jsonld(monkeypatch, tmp_path):
    prov_dir = tmp_path / "prov"
    config = ProvenanceConfig(prov_dir=str(prov_dir), context=True, frame = "results")

    @rule(name="just_outputs", config=config)
    def just_outputs(out: OutPath = OutPath("json_out.txt")):
        out.write_text("ok")

    monkeypatch.chdir(tmp_path)
    just_outputs()

    prov_files = list(prov_dir.glob("*.json"))
    assert prov_files

    prov_json = json.loads(prov_files[0].read_text())
    prov_id = prov_json["provenance"].keys().__iter__().__next__()
    assert "@graph" in prov_json
    assert prov_json["@context"]["provenance"]["@container"] == [
        "@graph",
        "@id",
    ]
    assert prov_id in prov_json["provenance"]


def test_prov_results_frame_trig(monkeypatch, tmp_path):
    prov_dir = tmp_path / "prov"
    config = ProvenanceConfig(prov_dir=str(prov_dir), out_fmt="trig", frame = "results")

    @rule(name="graph_rule", config=config)
    def graph_rule(out: OutPath = OutPath("trig_out.txt")):
        out.write_text("ok")

    monkeypatch.chdir(tmp_path)
    graph_rule()

    prov_files = list(prov_dir.glob("*.trig"))
    assert prov_files

    dataset = Dataset()
    dataset.parse(data=prov_files[0].read_text(), format="trig")
    contexts = [ctx for ctx in dataset.graphs() if ctx != dataset.default_context]
    prov_context = next(
        (
            ctx
            for ctx in contexts
            if str(ctx.identifier).endswith(f"prov-{graph_rule.__name__}")
        ),
        None,
    )
    assert prov_context is not None
    assert len(dataset.get_context(prov_context.identifier)) > 0
    assert len(dataset.default_context) == 0
