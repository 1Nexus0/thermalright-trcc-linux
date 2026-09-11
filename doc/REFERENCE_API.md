# API reference

**Generated — do not edit.** `PYTHONPATH=src python3 dev/gen_api_reference.py`

A REST interface to the same command bus every other UI uses. Each endpoint builds a Command, dispatches it, and returns the Result as JSON — so anything here is also reachable from the CLI, the GUI, or your own client. The Commands themselves are documented in [`REFERENCE_COMMANDS.md`](REFERENCE_COMMANDS.md).

**3 endpoints.**

## Running it

```bash
trcc api                              # http://127.0.0.1:8080
trcc api --port 9000                  # another port
trcc api --token random:32            # require X-API-Token
trcc api --host 0.0.0.0 --token ...   # a public bind REQUIRES a token
```

Without `--token` on loopback the API is unauthenticated (dev mode). Binding any other interface without one is refused rather than allowed — an open device-control API on a LAN is not a default worth having. `--pair` prints a one-time 6-character code that a remote device exchanges for the token via `POST /pair`.

Devices are addressed by **key** — the `vid:pid` string, e.g. `0402:3922` — the same identifier the CLI and the wire use. Every response carries `ok` and `message`.

Interactive docs are served at `/docs` while the API is running.

## Meta

| Endpoint | Returns | Description |
|---|---|---|
| `GET /` | `dict` | — |
| `GET /health` | `dict` | Liveness probe — always reachable, no auth required. |
| `POST /pair` | — | Exchange the terminal pairing code for the persistent API token. |
