# hermes-gauge demo

Dashboard that ships in this repo next to the plugin. It is not part of the plugin package.

## Run

```bash
python3 server.py
```

Default port is **8787**. From the repo root, `make demo` uses port **8788** (`GAUGE_PORT`) and checks that `/` and `/health` return HTTP 200.

```bash
GAUGE_PORT=8788 python3 server.py
```

## Endpoints

- `GET /` — the dashboard
- `GET /health` — `{"ok": true}`
- `GET /api/gauge` — the plugin database when it exists, otherwise fixture sessions
- `GET /api/gauge?fixture=1` — four fixture sessions at 92%, 63%, 38%, and 12%
- `GET /api/gauge?demo=1` — six fictional sessions, two per band. Nothing is written to disk

## Fixture mode

`GAUGE_FIXTURE=1`, or no Hermes database on the machine, serves the four fixture sessions. Hermes does not need to be installed, and the server does not write a database. `GAUGE_LIVE=1` reads the plugin database only.
