"""Demonstrate configuring provenance context and running in an isolated session."""

from makeprov import InPath, OutPath, Session, ProvenanceConfig, main, rule

# Pin the base IRI for identifiers and write provenance beneath examples/prov
ProvenanceConfig.set(
    ProvenanceConfig.get().clone_with(
        base_iri="https://example.org/workflows/context-demo",
        prov_dir="examples/prov/context-demo",
    )
)

session = Session()


@rule(session=session)
def extract_snippet(
    source: InPath = InPath("README.md"),
    snippet: OutPath = OutPath("examples/context_demo/snippet.txt"),
):
    """Write a short excerpt from the repository README."""
    lines = source.read_text(encoding="utf-8").splitlines()
    snippet.write_text("\n".join(lines[:5]), encoding="utf-8")


@rule(session=session)
def summarize_snippet(
    snippet: InPath = OutPath("examples/context_demo/snippet.txt"),
    summary: OutPath = OutPath("examples/context_demo/summary.txt"),
):
    """Summarize the excerpt and return a structured note."""
    text = snippet.read_text(encoding="utf-8")
    words = [w for w in text.split() if w.strip()]
    summary.write_text(
        f"Captured {len(words)} words across {len(text.splitlines())} lines.\n",
        encoding="utf-8",
    )
    return {
        "id": "https://example.org/workflows/context-demo/summary",
        "type": "prov:Entity",
        "comment": "Summary of README excerpt",
    }


if __name__ == "__main__":
    main(session=session)
