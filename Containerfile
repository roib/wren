FROM docker.io/library/python:3.12-slim-bookworm

# ffmpeg: merging, audio re-encode, thumbnails
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# JS runtime yt-dlp uses to solve YouTube's player challenges
COPY --from=docker.io/denoland/deno:bin-2.5.4 /deno /usr/local/bin/deno

WORKDIR /app

# Dependencies first so code changes don't reinstall them
COPY pyproject.toml ./
RUN pip install --no-cache-dir "yt-dlp[default]" fastapi "uvicorn[standard]" pillow pywebpush tzdata

COPY wren ./wren
COPY web ./web
COPY start.sh ./
RUN pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 1000 wren \
    && mkdir -p /data \
    && chown wren:wren /data
USER wren

ENV WREN_DATA=/data \
    WREN_WEB=/app/web \
    WREN_TZ=America/Los_Angeles \
    TZ=America/Los_Angeles \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

CMD ["/app/start.sh"]
