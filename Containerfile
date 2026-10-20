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

# Finding ad reads: Whisper (speech to text, on the CPU) and the Claude API
# client. Pinned: these are the versions tested together.
RUN pip install --no-cache-dir "faster-whisper==1.2.1" "anthropic==1.15.0"
# The Whisper model, baked in so the server never downloads it (~150 MB).
RUN python -c "from faster_whisper import download_model; download_model('base.en', output_dir='/opt/whisper/base.en')"

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
    WREN_WHISPER_MODEL=/opt/whisper/base.en \
    HF_HUB_OFFLINE=1 \
    WREN_TZ=America/Los_Angeles \
    TZ=America/Los_Angeles \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

CMD ["/app/start.sh"]
