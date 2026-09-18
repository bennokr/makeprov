from __future__ import annotations

import re
import subprocess
from pathlib import Path
import shutil

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # 3.10
    import tomli as tomllib

from sphinx.cmd.build import main as sphinx_main

# Matches whatever w3id.htaccess's own versioned-redirect rule matches
# (`context/([0-9]+(?:\.[0-9]+)*)`), so "v0.8" and "v0.8.1" are both valid
# release tags, not just three-part versions.
_TAG_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def _tagged_context_versions(repo_root: Path) -> dict[str, str]:
    """Map released version -> JSON-LD context content at that tag.

    Sourced straight from git tags rather than duplicate files checked into
    ``docs/``, so a versioned context (see ``w3id.htaccess``) never drifts
    from what was actually published at that version and never needs manual
    upkeep. A tag missing ``src/makeprov/context.jsonld`` (pre-dating the
    file's current location) is skipped rather than failing the build.
    """
    try:
        tags = subprocess.run(
            ["git", "tag", "--list"], cwd=repo_root, check=True,
            capture_output=True, text=True,
        ).stdout.splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}

    versions = {}
    for tag in tags:
        match = _TAG_RE.match(tag.strip())
        if not match:
            continue
        show = subprocess.run(
            ["git", "show", f"{tag}:src/makeprov/context.jsonld"],
            cwd=repo_root, capture_output=True, text=True,
        )
        if show.returncode == 0:
            versions[match.group(1)] = show.stdout
    return versions


def main() -> None:
    # This file is docs/_build_docs.py
    repo_root = Path(__file__).resolve().parents[1]
    pyproject_path = repo_root / "pyproject.toml"

    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data["project"]

    project_name = project["name"]
    release = project["version"]
    package_name = project_name.replace("-", "_")

    docs_source = repo_root / "docs"
    docs_build = docs_source / "_build" / "html"
    if docs_build.exists():
        shutil.rmtree(docs_build)
    docs_build.mkdir(parents=True, exist_ok=True)

    autosummary_dir = docs_source / "_autosummary"
    if autosummary_dir.exists():
        shutil.rmtree(autosummary_dir)
    autosummary_dir.mkdir(parents=True, exist_ok=True)

    # Publish JSON-LD context into build output: "latest" plus one file per
    # released version, so the w3id.htaccess versioned redirects
    # (/context/X.Y.Z -> context-X.Y.Z.jsonld) keep resolving across
    # rebuilds instead of only ever serving the most recently built version.
    context_src = repo_root / "src" / "makeprov" / "context.jsonld"
    if context_src.exists():
        shutil.copyfile(context_src, docs_build / "context.jsonld")
        shutil.copyfile(context_src, docs_build / f"context-{release}.jsonld")

    for version, content in _tagged_context_versions(repo_root).items():
        (docs_build / f"context-{version}.jsonld").write_text(content, encoding="utf-8")

    args = [
        "-b",
        "html",
        "-D",
        f"package_name={package_name}",
        "-D",
        f"project={project_name}",
        "-D",
        f"release={release}",
        str(docs_source),
        str(docs_build),
    ]

    raise SystemExit(sphinx_main(args))


if __name__ == "__main__":
    main()
