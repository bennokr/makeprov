import os
import sys
import glob

# sphinx-build -b html -D package_name="cool_project" -D project="Cool Project" -D release="1.2.3" docs docs/_build/html

# pip install sphinx myst-parser[linkify] sphinx-rtd-theme

# --- make your package importable ---
sys.path.insert(0, os.path.abspath("../src"))


# --- extensions ---
extensions = [
    "myst_parser",          # Markdown + MyST
    "sphinx.ext.autodoc",   # pull in docstrings
    "sphinx.ext.autosummary",  # generate API pages
    "sphinx.ext.napoleon", # docstrings
    "sphinx.ext.githubpages",
]

# Parse both .rst and .md
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# Root document
root_doc = "index"          # Sphinx ≥4
master_doc = "index"        # backward compatibility

# Common paths
templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
html_static_path = ["_static"]

# MyST options (optional but common)
myst_enable_extensions = [
    "linkify",
]

# Automatically generate autosummary stub files
autosummary_generate = True
autosummary_imported_members = True

# (Optional) slightly nicer autodoc defaults
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}

html_theme = "sphinx_rtd_theme"