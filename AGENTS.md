# Repository Guidelines

## Project Structure & Module Organization

`main.py` launches the desktop application and FastAPI service. Backend code lives in `backend/`: API routes are in `backend/api/`, download and media orchestration in `backend/core/`, persistence in `backend/database/`, and the plugin framework in `backend/plugins/`. Site extractors are standalone modules under `plugins/`; use `plugins/example.py` as the starting point for new integrations. The React/Vite UI is in `frontend/src/`, with static assets in `frontend/public/`. Python regression tests live in `tests/`; screenshots and documentation assets belong in `screenshots/`.

## Build, Test, and Development Commands

- `pip install -r requirements.txt` installs backend/runtime dependencies.
- `playwright install chromium` installs the optional browser fallback used by some extractors.
- `python main.py` runs the packaged desktop-style application locally.
- `python -m uvicorn backend.app:app --reload` runs the API with reload for backend development.
- `python -m unittest discover -s tests -v` executes the Python regression suite.
- `cd frontend && npm install` installs frontend dependencies.
- `cd frontend && npm run dev` starts the Vite development server.
- `cd frontend && npm run lint` checks React/JavaScript style.
- `cd frontend && npm run build` creates the production frontend bundle.

## Coding Style & Naming Conventions

Use four spaces for Python and two spaces for JavaScript/JSX. Follow `snake_case` for Python functions and modules, `PascalCase` for classes and React components, and `camelCase` for JavaScript variables. Keep extractors asynchronous, give each one a descriptive `*Extractor` class, and declare supported hosts in `URLS`. Use `backend.plugins.utils.bounded_map` instead of creating unbounded request lists. Run ESLint before submitting frontend changes.

## Testing Guidelines

Tests use Python's `unittest` framework. Name files `test_*.py` and methods `test_<behavior>`. Add regression tests for path safety, plugin routing, concurrency limits, and atomic downloads when those areas change. There is no enforced coverage threshold; prioritize failure-prone pipeline behavior and avoid tests that modify the real download database or user files.

## Commit & Pull Request Guidelines

Prefer concise, imperative subjects following the existing pattern: `feat: add ...`, `fix: prevent ...`, or `docs: update ...`. Keep commits focused. Pull requests should explain the user-visible effect, list verification commands, link relevant issues, and include screenshots for UI changes. Call out new domains, browser requirements, database migrations, or configuration keys explicitly.

## Security & Configuration Tips

Never commit credentials, downloaded media, task logs, local databases, or machine-specific settings. Preserve path-containment checks, validate remote URLs, and keep network concurrency bounded when editing download or proxy code.
