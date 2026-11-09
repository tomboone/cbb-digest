#!/usr/bin/env bash
# Runs before the checks. alembic.ini points the alembic CLI at sqlite:///data/cbb-digest.sqlite3,
# relative to the repo root, and SQLite won't create the directory; the drift check needs it.
set -euo pipefail
mkdir -p data
