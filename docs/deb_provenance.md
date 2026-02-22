# Debian Artifact Provenance

The CI packaging workflow builds unsigned internal `.deb` artifacts and publishes:

- `.deb` package
- `.sha256` checksum
- `.provenance.json` metadata

Workflow path:

- `.github/workflows/build-deb.yml`

Builder script:

- `scripts/build_deb.sh`

## Versioning Scheme

Version is enforced as Debian-compatible:

- Tag builds (`vX.Y.Z`): `X.Y.Z` (with `-` normalized to `~` if present)
- Branch/manual builds: `0.1.0~gitYYYYMMDDHHMM.<shortsha>`

The script fails if the generated/provided version is not Debian-safe.

## Provenance Metadata

`*.provenance.json` includes:

- package name/version/architecture
- source commit SHA
- build timestamp
- GitHub run identifiers (`run_id`, `workflow`, `ref`, etc.)
- artifact filenames

This provides baseline provenance for internal artifacts before package signing is added.
