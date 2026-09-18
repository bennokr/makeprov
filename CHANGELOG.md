# Changelog

## 0.8

- Added `ProvenanceConfig(record_environment=True)` (CLI: `--record-environment`)
  to record retrospective environment evidence alongside the existing
  declared-dependency specs: the distributions a run actually imported,
  pinned to exact installed versions, and a citation of any lockfile found
  (`uv.lock`, `poetry.lock`, `Pipfile.lock`, `pdm.lock`, `pylock.toml`) hashed
  and linked via `prov:wasDerivedFrom`. Off by default.
- The runtime agent now records `operatingSystem` (from `/etc/os-release`
  where available), a more informative environment descriptor than a raw
  kernel `uname` string.
- `ProvMeta[T]` annotates a rule argument to record its bound value as a
  `schema:additionalProperty` entry on the activity.
- `ProvenanceConfig(stream=True)` appends each finished activity to a
  recovery `.jsonl` file; with `merge=True` (the default) the final merged
  document replaces it atomically on success, and the `.jsonl` is kept
  otherwise so an interrupted run isn't silently lost.
- Every activity now carries `schema:duration`, an ISO 8601 duration derived
  from its existing timestamps.

## 0.7

The decorator API is unchanged — existing `@rule` functions using
`InPath`/`OutPath` keep working — but the emitted graph differs:

- The script is no longer a `prov:SoftwareAgent`. It is a `prov:Plan`, reached
  from the activity via `prov:qualifiedAssociation`/`prov:hadPlan`. Consumers
  that looked for `prov:wasAssociatedWith` to find the script should follow
  `prov:hadPlan` instead.
- Outputs now carry a `sha256` content digest. Previously the digest was
  computed and then discarded for outputs.
- Entity IRIs derived from a GitHub remote are pinned to the **commit** rather
  than the branch, so an IRI no longer denotes different bytes after each push.
- Run identifiers include seconds and a random suffix. Minute-resolution ids
  meant two runs of the same rule in one minute shared an activity IRI.
- A declared output that is missing after a successful run now raises
  `UnresolvedArtifactError` instead of being dropped from the graph. Missing
  *inputs* are recorded without content metadata and logged, rather than
  disappearing.
- `Prov.create()` takes `list[ArtifactRef]` instead of `list[Path]`.
- The Snakemake bridge follows the same model: each rule is now a `prov:Plan`
  at `<base>rule/<name>`, and each job activity carries a
  `prov:qualifiedAssociation`. Its agent node gained `schema:SoftwareApplication`.
- The bridge no longer defaults to a `urn:snakemake:` namespace. That NID was
  never IANA-registered, so it named no real namespace and collided across
  unrelated workflows sharing rule names. It now shares the decorator API's
  identifier policy (`makeprov.prov.resolve_iris`): an explicit `base_iri`,
  else a commit-pinned base derived from a GitHub remote, else relative IRIs.
- `blob:` identifiers are only minted for files inside the repository. An
  absolute path within the checkout is rewritten to its repo-relative form, and
  a path outside it gets a `file:` URI instead of a `blob:` IRI that would
  expand to a nonexistent location.
- Entities carry `schema:sha256` (bare hex) alongside the algorithm-qualified
  `dct:identifier`, so consumers no longer have to parse a prefix.
- Activities state `prov:generated` as well as each entity's inverse
  `prov:wasGeneratedBy`, mirroring `prov:used` and mapping onto RO-Crate's
  `result`.
- A dirty working tree is recorded as `<sha>-dirty` (the `git describe --dirty`
  convention) with a warning, instead of asserting a clean revision that does
  not describe what ran.
- `prov:wasDerivedFrom` and `rdfs:seeAlso` on cached downloads are emitted as
  node references rather than string literals, so the links are traversable.
  `CachedDownload(..., sha256=...)` pins and verifies the cached copy.
- The base heuristic covers all hosts in `forges.toml`, not just GitHub, and
  understands SSH remotes. Credentials embedded in a remote URL are stripped —
  previously a remote like `https://user:token@github.com/o/r.git` would have
  put the token into `@base` in every document.
