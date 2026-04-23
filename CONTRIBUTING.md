# Contributing

Thanks for taking an interest in the project. This dashboard is a small hobby codebase — contributions that fix real user pain, add tests, or improve clarity are all welcome.

## Setup

```bash
git clone https://github.com/jtspetersen/abrp-dashboard.git
cd abrp-dashboard
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Running the app

```bash
python -m streamlit run app.py
```

Upload any ABRP `.xlsx` trip export via the sidebar. A sample is included at `data/sample_trip.xlsx`.

## Running the tests

```bash
python -m pytest
```

The suite is offline-only and runs in about 1 second. It covers the parser, calculations, enrichment helpers (haversine / geometric estimate / Supercharger matcher / exception hierarchy), and the elevation grid (using a synthetic fixture so no `.npz` is needed).

If you're touching the map, elevation profile, or Streamlit layout, there's no automated test — verify manually by running the app and uploading the sample trip.

## Style

- **PEP 8** with a 100-char line limit (enforced by ruff). Run `ruff check .` and `ruff format .` before pushing.
- **Docstrings** on public functions. Explain *why* something non-obvious is done, not *what* the code says.
- **Type hints** on new code. We don't yet enforce with `mypy --strict`, but we're heading that way.
- **No dead code.** If you remove a feature, remove the code. Don't leave commented-out blocks.
- **No new runtime API keys.** The project is deliberately keyless — Photon and OSRM are both free public services. If a change needs a new service, prefer self-hosting data (like we do for elevation) or discuss in an issue first.

## Opening an issue before a PR

For anything larger than a small fix or docs tweak, open an issue first. It's cheaper for both sides to align on approach before you write code. Describe the problem you're solving and how you'd like to solve it; if you're not sure, say so — I'd rather talk than review a wrong-direction PR.

## Commit messages

- First line: short imperative summary (≤ 72 chars). Think "fix X" / "add Y" / "refactor Z".
- Empty line, then free-form body if there's context worth preserving (why the change was needed, alternatives considered, gotchas).

## Architecture orientation

See [`docs/architecture.md`](docs/architecture.md) for a walkthrough of the modules and dataflow.
