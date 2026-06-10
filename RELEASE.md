# Release

What a release of `jupyterlab_share_files_extension` consists of and how it is produced. One version number covers every artefact - npm, PyPI and the git tag always agree.

## What goes into a release

- **npm package** - `jupyterlab_share_files_extension@<version>` ([registry](https://www.npmjs.com/package/jupyterlab_share_files_extension)): the prebuilt labextension (frontend)
- **PyPI package** - `jupyterlab-share-files-extension <version>` ([registry](https://pypi.org/project/jupyterlab-share-files-extension/)): wheel + sdist carrying the server extension, the `jupyterlab_share_files` CLI, the `tunnel` library and the bundled labextension - `pip install` alone gives the full feature set
- **Git tag** - `RELEASE_v<version>` on the release commit
- **GitHub release** - published against the tag, body mirroring the version's `CHANGELOG.md` section
- **CHANGELOG.md section** - `## [<version>] - <date>` with the content grouped under Added / Changed / Fixed; the canonical summary of the release
- **Journal entry** - `.claude/JOURNAL.md` entry stamped with the released version (rationale and verification record)

## Gates - no publish before all pass

- Journal entry written and `journal-tools check` clean
- CHANGELOG section for the version in place
- `jlpm prettier` + `jlpm run lint:check` exit 0 (same checks CI runs)
- Backend (pytest) and frontend (jest) test suites green

## Process

- Releases run through the `/release-jupyterlab-extension` command: journal → changelog → lint → `make publish` → commit + push → report
- `make publish` bumps the PATCH version, builds, publishes to npm and PyPI, and auto-commits the package metadata
- The release content commit follows conventional-commit format and describes the changes, not the version bump
- Versioning: PATCH auto-increments per publish; MINOR/MAJOR only on explicit decision

## Verification

- npm and PyPI are confirmed live (registry queries) before the release is reported
- CI (GitHub Actions: Build, Check Release) must be green on the release commit
- Cloudflare-affecting releases are verified live against the running tunnel (see `docs/acc-crit-cloudflare-integration.md` verification log)
