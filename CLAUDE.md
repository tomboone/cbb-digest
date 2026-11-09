# cbb-digest

Weekly men's college basketball digest email, filtered through a personal watchlist of NBA draft prospects. Runs on `kumadrome` as one long-running container. Modelled on nba-today (`~/projects/nba-today`, tomboone/nba-today); port its modules by copy-and-rename, never a shared package.

## Linear

- linear_initiative: cbb-digest
- linear_team: NEU
- linear_repo: cbb-digest

## Docs

- specs_dir: docs

Project spec: `docs/specs/cbb-digest-v1-weekly-draft-prospect-digest-project-spec.md`
Original brief: `docs/CBB Weekly Digest — Handoff for Claude Code.md` (superseded where the spec disagrees)
Glossary: `CONTEXT.md`. Decisions: `docs/adr/`.

## Toolchain

uv, ruff, pyright, pytest. Python 3.13. `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` before opening a PR.

## Branch flow

Work branches are cut from `main`, and PRs target `main` as merge commits. Every push to `main` whose `ci` check passes publishes `ghcr.io/tomboone/cbb-digest` (`latest` while that commit is still the tip of `main`), which Watchtower deploys to kumadrome. CI, image publishing and dependency automation call the shared `neuroticsasquat-ch/workflows@v1` (ops spec `docs/specs/ops-shared-ci-workflows-project-spec.md`).
