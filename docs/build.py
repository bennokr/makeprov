from __future__ import annotations

from pathlib import Path
import shutil

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # 3.10
    import tomli as tomllib

from sphinx.cmd.build import main as sphinx_main


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
