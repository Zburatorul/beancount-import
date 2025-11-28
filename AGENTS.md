# Repository Guidelines

## Project Structure & Module Organization
- `beancount_import/` holds the Python package, including source modules, configuration helpers, and the generated `frontend_dist/` assets that get served alongside the web server.
- `frontend/` contains the TypeScript/React UI, the node-based build configuration, and third-party assets (see `frontend/README.md` for more detail).
- `examples/`, `test_smart_importer/`, and `testdata/` provide runnable scripts, named tests, and golden fixtures used by the importers.
- `scripts/`, `notebooks/`, and `useful_commands` are supplemental tooling and documentation drivers; treat them as convenience references rather than primary modules.
- Published metadata lives in `pyproject.toml`, `setup.py`, and `beancount_import.egg-info/`; packaging follows standard setuptools layouts.

## Build, Test, and Development Commands
- `pip install -e .` (root): install in editable mode with Python deps; preferred for development so code changes take effect immediately.
- `pip install .` or `pip install beancount-import` for production installs that include the prebuilt frontend.
- `pytest` (root): runs the entire Python test suite; honors `pytest.ini` so it skips `frontend/` and `examples/`.
- `BEANCOUNT_IMPORT_GENERATE_GOLDEN_TESTDATA=1 pytest`: regenerates golden files under `testdata/`; compare `git diff` before adding updates.
- `tox` (root): executes `mypy` + `pytest` + coverage via the `[testenv]` commands; use to validate type coverage and ensure CI parity.
- `npm install` then `npm run builddev` (inside `frontend/`): builds the frontend into `beancount_import/frontend_dist/`; use `npm run builddev:watch` while editing UI components.

## Coding Style & Naming Conventions
- Follow idiomatic Python with 4-space indentation, snake_case for functions/modules, and PascalCase for dataclasses and exceptions.
- Type annotations are expected; `tox` already runs `mypy beancount_import --install-types --non-interactive --disable-error-code=type-abstract`, so keep signatures precise and avoid `Any` unless there is a justification comment.
- React components follow the existing `.tsx` files’ patterns; prefer descriptive prop names and keep stylesheet changes centralized in `frontend/styles.css`.
- Brief comments may clarify complex logic but avoid restating the obvious; docstrings are welcome for public functions.

## Testing Guidelines
- Core tests live in `test_smart_importer/` and the specialized golden suites under `testdata/source/...`.
- Keep tests fast and deterministic; the `tox` workflow also runs coverage, so rely on `pytest` + `coverage run -m pytest -vv` for CI parity.
- When you need to update a golden file, set `BEANCOUNT_IMPORT_GENERATE_GOLDEN_TESTDATA=1 pytest`, inspect the modified fixture in `testdata/`, and then rerun `pytest` to confirm nothing else regressed.
- Name new tests to describe the scenario (`test_<feature>_<condition>`); keep helper data near the test that consumes it.

## Commit & Pull Request Guidelines
- Mirror the existing commit log by using short imperative titles (e.g., `Fix Venmo balance import`), and add a body or bullet list only when additional context or issue references are needed.
- Link GitHub issues or RFCs when relevant, and mention major user-visible changes in the PR description.
- PRs should summarize the scope, note the commands used (e.g., `tox`, `pytest`, `npm run builddev`), and include screenshots only when the frontend UI changes; tag maintainers or reviewers if you need extra eyes.
- Run `tox` or the subset of commands that cover your changes before opening a PR so CI has a clean baseline.
