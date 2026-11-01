# CBB Digest

A weekly men's college basketball digest email, filtered through a personal watchlist of NBA draft
prospects. Every Monday morning in season it reports what the watchlist did last week, which games
this week are worth watching, which teams moved in the top 25, and which freshmen not on the list
are playing big minutes.

cbb-digest is a single long-running Python service, deployed as one container on a self-hosted box.
It builds the digest from Torvik player data, CollegeBasketballData team ratings and ESPN's
schedule, and delivers it through Resend, with SMTP as a fallback.

See [`CONTEXT.md`](CONTEXT.md) for the glossary, [`docs/specs/`](docs/specs/) for the project spec
and [`docs/adr/`](docs/adr/) for the design decisions. The watchlist is
[`watchlist.yaml`](watchlist.yaml).

## Commands

| Command | Does |
|---|---|
| `cbb-digest fetch` | Fetches every source for the run window into a 24 h cache |
| `cbb-digest load` | Parses the cache and upserts it into SQLite |
| `cbb-digest resolve` | Resolves the watchlist against Torvik's player file |
| `cbb-digest compute` | Builds the digest model |
| `cbb-digest render [--dry-run]` | Renders the digest to HTML and text |
| `cbb-digest send [--force]` | Sends the digest |
| `cbb-digest run [--week YYYY-MM-DD] [--force] [--dry-run]` | All of the above for one week |
| `cbb-digest serve` | Runs the weekly scheduler loop (the container entrypoint) |
| `cbb-digest reset --season N [--send]` | Builds the portal-reset digest for a new season |
| `cbb-digest health` | Exits 0 if the scheduler has been active in the last 8 days |

## Running locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13. Configuration is read from the
environment; [`.env.example`](.env.example) documents every variable.

```sh
uv sync
uv run cbb-digest --help
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

## License

MIT; see [`LICENSE`](LICENSE).

Data: Bart Torvik, CollegeBasketballData, ESPN. Not affiliated with the NCAA.
