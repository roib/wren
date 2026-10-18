#!/bin/sh
# Container entrypoint. YouTube changes often and yt-dlp keeps up, so fetch the
# newest release on every start (a weekly restart on the host keeps it fresh).
# If PyPI can't be reached, the copy built into the image is used.
if [ "${WREN_UPDATE_YTDLP:-1}" = "1" ]; then
  if pip install --quiet --no-cache-dir --disable-pip-version-check --upgrade \
      --target /tmp/ytdlp "yt-dlp[default]" 2>/tmp/ytdlp-update.log; then
    export PYTHONPATH=/tmp/ytdlp
  else
    echo "wren: couldn't update yt-dlp, using the built-in one" >&2
    tail -n 3 /tmp/ytdlp-update.log >&2
  fi
fi
exec wren serve --host 0.0.0.0 --port 8000
