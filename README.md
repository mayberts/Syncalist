# Syncalist

Keeps music playlists in sync between the users of a Plex Home. The Home admin picks
which playlists are shared and with whom. Once shared, anyone can edit their copy and
the changes reach everyone.

See [PLAN.md](PLAN.md) for the design.

**Status: M3.** Sign in, share music playlists between Home users (merge, replace or keep
separate), and keep them in sync automatically. Backups are taken before anything is removed.

## Install on Unraid

The image is published to `ghcr.io/mayberts/syncalist:latest` by GitHub Actions on every push.

1. On the Unraid server, copy the template into the user templates folder (from the Unraid terminal):
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-syncalist.xml \
     https://raw.githubusercontent.com/mayberts/Syncalist/claude/vibrant-fermi-feca8d/unraid/syncalist.xml
   ```
2. **Docker** tab → **Add Container** → choose **syncalist** from the *Template* list.
3. Check the Plex URL, then **Apply**.
4. Open the web UI from the container's icon and sign in with the Plex account that owns the server.

Data is stored in `/mnt/user/appdata/syncalist`. If you use Docker Compose on Unraid instead of the template, use [`unraid/docker-compose.yml`](unraid/docker-compose.yml), which maps the same folder. The container runs as `PUID`/`PGID` (99/100 by default on Unraid).

If the repository is private, the image is private too. Either make the package public
(GitHub → your profile → **Packages** → *syncalist* → **Package settings** → **Change visibility**),
or sign in on Unraid first: `docker login ghcr.io -u mayberts` with a personal access token that has `read:packages`.

## Run with Docker

```bash
docker compose up -d
```

This pulls the published image. To build locally instead, swap `image:` for `build: .` in `docker-compose.yml`.

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
| `PUID` / `PGID` | `1000` / `1000` | User and group the app runs as; owns the files in the data folder |

## Tests

```bash
pytest
```

The tests use a fake Plex server, so no real server is needed.
