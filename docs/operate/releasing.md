---
audience:
  - developer
  - operator
doc_type: how-to
product_area: operate
stability: beta
prerequisites:
  - Push access to the repository and the GitHub CLI (`gh`) signed in as the owner
reviewed_on: 2026-10-14
version_applicability: 0.1.0a1 alpha
tags:
  - release
  - versioning
  - alpha
---

# Releasing

How a CALIBER release is cut. It is manual on purpose: there is no tag-triggered workflow, because the alpha
ships a handful of wheels to a handful of testers and a person should look at the commit before it goes out.
Automating it is on the post-alpha backlog.

A release is **four wheels and their sdists, attached to a GitHub prerelease, on a tag**. Nothing is published
to PyPI; the install page tells testers to install from the downloaded wheels.

## The names

| Thing | Spelling | Why |
| --- | --- | --- |
| Git tag | `caliber-v0.1.0a1` | the repository's `caliber-v*` prefix plus the PEP 440 version the wheels carry |
| Python packages | `0.1.0a1` | `caliber-suite`, `caliber-sdk`, `caliber-cli`, `caliber-plugin-sdk`, all the same version |
| `caliber-ui/package.json` | `0.1.0-alpha.1` | npm needs semver; the same release spelled that way |

`tests/test_version_lockstep.py` fails if any of the four `pyproject.toml` files, their `__version__`
constants, `package.json` or the lockfile's root entry disagree. Bump all of them in one commit.

The alpha is a **prerelease**. Formal product tags (`caliber-v0.1.0`, `caliber-v1.0.0`) are for the roadmap's
M4 and M6 gates and are a different thing.

## Before you tag

1. **The version agrees everywhere** (the lockstep test above passes on the commit you will tag).
2. **`caliber/CHANGELOG.md` has a dated section** for the version, and it links
   [Alpha scope and limitations](../start/alpha-scope.md).
3. **The commit is green.** On `main`, every check in the ledger passed, and in particular `Backend core
   journey`, `Alpha journey (browser)`, `Alpha install (wheels, no checkout)` and `Wheel build (with bundled
   SPA)`. The full backend suite (it runs after merge, not on PRs) and the `Docker stack smoke` workflow are
   green too; run the latter by hand if the last run is more than a week old:
   `gh workflow run stack-smoke.yml --ref main`.
4. **The gap list is closed or its deferrals are written down.** `docs/caliber-alpha-gaps.md` section 8 is the
   checklist. Anything left open is named in the release notes, not left for a tester to find.
5. **The licence is one licence.** `LICENSE`, `caliber/LICENSE`, the packages' metadata and the README must
   name the same one before a public tag.
6. **The release notes are written** (`docs/reports/release-0.1.0a1-notes.md`): what is in it, what is not,
   what was verified and how, and the known limitations.

## Cut it

From a clean checkout of the commit to tag:

```bash
git switch main && git pull --ff-only
git rev-parse HEAD                      # the commit you are about to tag; check it is the green one
git tag -a caliber-v0.1.0a1 -m "CALIBER 0.1.0a1 (alpha)"
git push origin caliber-v0.1.0a1
```

Build the artifacts from that tag, the way CI's `package` job does:

```bash
git switch --detach caliber-v0.1.0a1
npm --prefix caliber/caliber-ui ci
npm --prefix caliber/caliber-ui run build
rm -rf caliber/src/caliber/ui && mkdir -p caliber/src/caliber/ui && cp -R caliber/caliber-ui/dist/. caliber/src/caliber/ui/
python -m pip install build twine
rm -rf dist && mkdir dist
for pkg in caliber sdk/caliber-sdk sdk/caliber-cli sdk/caliber-plugin-sdk; do
  python -m build --outdir dist "$pkg"
done
twine check dist/*
ls dist                                  # four wheels and four sdists
scripts/verify-wheel-boot.sh --python python3.12 dist/caliber_suite-0.1.0a1-py3-none-any.whl
```

`verify-wheel-boot.sh` fails if the wheel has lost its bundled UI or its migrations, then installs it alone,
migrates, boots it and fetches the UI.

Publish the prerelease. `--latest=false` is explicit because a prerelease must not take the "Latest" badge:

```bash
gh release create caliber-v0.1.0a1 --prerelease --latest=false \
  --title "CALIBER 0.1.0a1 (alpha)" \
  --notes-file docs/reports/release-0.1.0a1-notes.md \
  dist/*
```

## Verify the release as a tester would

Download the assets into an empty directory and run the install script against them, in a clean virtual
environment, away from the checkout:

```bash
rm -rf /tmp/caliber-release && mkdir /tmp/caliber-release
gh release download caliber-v0.1.0a1 --dir /tmp/caliber-release
scripts/verify-alpha-install.sh --python python3.12 --wheel-dir /tmp/caliber-release
```

It installs the SDK, the CLI and the server from the wheels, migrates, starts MLflow and CALIBER, then asserts
readiness, an admin login, a project, a prompt and a real registry write. If it fails, the release is broken:
pull it (below) and fix forward.

## Housekeeping

The first tag, `caliber-v0.0.1`, was published as a normal release with no assets and holds the "Latest"
badge. Mark it as a prerelease so that the badge is not claimed by something that cannot be installed:

```bash
gh release edit caliber-v0.0.1 --prerelease --latest=false
```

## Pulling a bad release

Never reuse a tag number. Delete the release and the tag, then release the fix as the next number
(`0.1.0a2`):

```bash
gh release delete caliber-v0.1.0a1 --yes --cleanup-tag
```

## What is not automated

Version bumps, the changelog, the notes, the licence check and the decision that a commit is good enough. The
build and verification steps above are the same commands CI runs, so a future `release.yml` would assemble
them: build on tag, run both verify scripts, and attach the assets.
