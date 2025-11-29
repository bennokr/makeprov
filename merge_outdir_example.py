"""Example using OutDir/InDir and merged provenance buffers."""

from makeprov import InDir, OutDir, OutPath, build, rule


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
    main_content = source_dir.file("main.txt")

    render_fragment("logo", dest=logo)
    report.write_text(main_content.read_text())
    index.write_text("<html><body>see report.md</body></html>\n")


if __name__ == "__main__":
    # Build a single sample and emit one provenance record for the entire call tree
    build("site/1/")
