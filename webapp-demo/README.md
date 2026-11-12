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

## Who can reach it

The server listens on `127.0.0.1` only. In its default mode it serves live per-session data (ids, models, fill), so it does not leave your machine unless you say so. `--host` names another interface and then `--token` (or `GAUGE_TOKEN`) is mandatory; without a token the server refuses to start.

```bash
python3 server.py --host 0.0.0.0 --token "$(openssl rand -hex 16)"
```

Every request must carry the token: `Authorization: Bearer <token>`, or `?token=<token>` in the URL, which also sets a cookie so the page keeps working. A request without it gets a 401 and no data.

## Endpoints

- `GET /` — the dashboard
- `GET /health` — `{"ok": true}`
- `GET /api/gauge` — the plugin database when it exists, otherwise fixture sessions
- `GET /api/gauge?fixture=1` — four fixture sessions at 92%, 63%, 38%, and 12%
- `GET /api/gauge?demo=1` — six fictional sessions, two per band. Nothing is written to disk

## Fixture mode

`GAUGE_FIXTURE=1`, or no Hermes database on the machine, serves the four fixture sessions. Hermes does not need to be installed, and the server does not write a database. `GAUGE_LIVE=1` reads the plugin database only.
