FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SYNCALIST_DATA=/config \
    PORT=8585 \
    PUID=1000 \
    PGID=1000

WORKDIR /app
COPY pyproject.toml README.md ./
COPY syncalist ./syncalist
RUN pip install --no-cache-dir . && mkdir -p /config
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/mayberts/Syncalist/HEAD/unraid/icon.png" \
      net.unraid.docker.webui="http://[IP]:[PORT:8585]/" \
      org.opencontainers.image.title="Syncalist" \
      org.opencontainers.image.description="Share and sync music playlists between Plex Home users"

VOLUME /config
EXPOSE 8585
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "syncalist"]
