# Syncalist

Keeps music playlists in sync between the users of a Plex Home. The Home admin picks
which playlists are shared and with whom. Once shared, anyone can edit their copy and
the changes reach everyone.

See [PLAN.md](PLAN.md) for the design.

**Status: M1, read-only.** You can sign in, connect your server and browse every Home
user's music playlists. Sharing and syncing come in M2 and M3.

## Run with Docker

```bash
docker compose up -d --build
```

Then open `http://<this-machine>:8585` and sign in with the Plex account that owns the server.

- Data (SQLite database and encryption key) is kept in `./data`. Back up both files together.
- The UI is meant for your home network only. Don't expose it to the internet.

## Run without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
SYNCALIST_DATA=./data python -m syncalist
```

## Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `SYNCALIST_DATA` | `/config` | Folder for the database and encryption key |
| `PLEX_URL` | — | Pre-fills the server address on first setup |
| `SYNC_INTERVAL` | `60` | Initial sync interval in seconds; changeable in the UI |
| `PORT` | `8585` | Web UI port |

## Tests

```bash
pytest
```

The tests use a fake Plex server, so no real server is needed.
