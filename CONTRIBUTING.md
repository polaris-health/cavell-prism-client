# Contributing

## Development setup

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
git clone https://github.com/polaris-health/cavell-prism-client
cd cavell-prism-client
uv sync                # installs the package + dev dependencies
uv run pytest          # 250+ hermetic tests, sub-second
uv run ruff check && uv run ruff format --check
uv run ty check
uv run pre-commit install   # ruff, ty, detect-secrets on every commit
```

The test suite is fully hermetic (no network) and guarded by
`pytest --timeout=60` — a test that accidentally makes a real HTTP call
fails loudly.

For end-to-end experiments, `docker compose up -d` starts a local HAPI FHIR
server on `http://localhost:8090`.

## Commits and branches

- Conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, ...) — a review
  convention, not an automated check. `commitlint.config.js` records the rules
  if you want to run it by hand (`npx commitlint --from HEAD~1`).
- `develop` is the default branch; all PRs target it.
- `main` is release-only: promotion PRs `develop → main` merge with a merge
  commit (enforced by ruleset + the `branch-guard` check).
- Every PR (develop and main) requires one **code-owner** approval
  (`.github/CODEOWNERS`), and authors cannot approve their own PRs. Repo
  admins can bypass the approval when merging a PR (`gh pr merge --admin`) —
  the bypass is recorded on the PR; direct pushes and force pushes stay
  impossible for everyone, and `v*` release tags can only be created by
  admins.

## Releases

The version lives in exactly one place: `src/cavell_client/__init__.py`
(`__version__`). `pyproject.toml` reads it via hatch dynamic versioning.

- **Nightlies** publish automatically from `develop` to PyPI as
  `<next-version>.devNNN` (skipped when develop hasn't changed). Install
  with `pip install --pre cavell-prism-client`.
- **Stable release**:
  1. Ensure `__version__` on develop is the version to release and
     `CHANGELOG.md` has its section.
  2. Open the promotion PR `develop → main`, merge it (merge commit).
  3. Tag the merge commit: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.
     The publish workflow verifies tag == version == on-main, builds, and
     publishes to PyPI via trusted publishing, then creates the GitHub
     release.
  4. **Immediately after**: open a PR to develop bumping `__version__` to
     the next minor — otherwise the next nightlies would sort below the
     just-published stable.

Package name `cavell-prism-client`, import name `cavell_client` — this is
intentional; don't "fix" one to match the other.
