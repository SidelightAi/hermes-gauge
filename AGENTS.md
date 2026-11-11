# AGENTS.md

Conventions for changing hermes-gauge.

## Layout

- `hermes-gauge/` is the Hermes Agent plugin. `webapp-demo/` is the standalone dashboard.
- Live readings come from `~/.hermes/plugin-data/hermes-gauge/data.db`.
- Keep the README line: "See how full your agent's head is."

## Python

The plugin and the webapp use the Python standard library only. Do not add pip dependencies, a lockfile, or third-party imports.

`pytest` runs the tests. It is not a plugin dependency. Do not import it from `hermes-gauge/` outside `hermes-gauge/tests/`.

## Public tools

Do not rename `gauge_check`, `gauge_report`, or `gauge_set_window`. `plugin.yaml` declares the tools. The product version is `0.6.1`. It lives in both `plugin.yaml` files, in `dashboard/manifest.json`, and in the panel title in `desktop/plugin.js`. Bump those four together.

## Status bands

`estimated_tokens = (chars // 4) * 2`, compared with the effective window. The effective window is the nominal window (default 200_000) times the family factor. An unknown model keeps 0.60 of the nominal window; a pro model keeps all of it. A provider token count replaces the character estimate.

- `OK` — ratio below 0.60
- `WATCH` — ratio at or above 0.60 and below 0.85
- `FULL` — ratio at or above 0.85, fill capped at 100%

## Demo mode

Demo mode is what the 90-second video shows. Keep the dark theme; add to it, do not restyle it.

Six fictional sessions, fullest first, two per band (`FULL`, `WATCH`, `OK`). The same rows live in both places:

- `webapp-demo/index.html` — injected by the **Demo data** button and by `?demo=1`
- `webapp-demo/server.py` — `GET /api/gauge?demo=1`, and that route does not write to disk

Real mode keeps reading the plugin database.

## Git

Conventional Commits: `type(scope): summary`. Types in use: `feat`, `fix`, `test`, `docs`, `chore`. One concern per commit.

Run the tests and get a passing result before every commit:

```bash
pytest hermes-gauge/tests/test_tools.py
```

`conftest.py` collects the hyphenated plugin directory as a plain directory so pytest does not import `hermes-gauge/__init__.py`. Leave that hook in place.

## Run the dashboard

```bash
cd webapp-demo && python3 server.py   # http://localhost:8787
```

Open `/?demo=1`, or click **Demo data**, to show the fictional sessions.
