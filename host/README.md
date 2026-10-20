# Finding ad reads with your Claude login (`claude -p`)

Wren can find ad reads in transcripts with Claude either through the API (an
`ANTHROPIC_API_KEY`, billed per request) or through this helper, which runs on
the host as your user and answers Wren's requests with `claude -p`, using your
Claude Code login. The container never sees a key or login: the two share files
in Wren's data folder.

```
container: Wren                       host: wren-claude-helper (your user)
  /data/ads/requests/<id>.json  ───►  claude -p --tools "" --json-schema …
  /data/ads/answers/<id>.json   ◄───
  /data/ads/helper.json         ◄───  "running, logged in" (every 10 minutes)
```

Claude runs with every tool turned off, from an empty directory, so whatever a
transcript says, all it can do is reply with text. Wren uses the helper while
`helper.json` is fresh and says it's logged in; otherwise it falls back to an
API key if it has one, else to waiting for SponsorBlock, and raises an alert.

Check Anthropic's current terms for unattended use of a subscription before
relying on this; an API key is the supported way to run Claude from a service.

## Setup

Needs `jq`, `flock` and `timeout` (util-linux/coreutils) on the host, and
lingering for your user (`loginctl enable-linger`) so the units run while
you're logged out.

With rootless podman the data folder belongs to the container's user, which
your own user can't write to; `wren-claude.service` therefore runs the helper
through `podman unshare` (as the container's root, which can). Wren creates
`ads/requests` and `ads/answers` when it starts, so start Wren once before
enabling the units.

1. Install Claude Code (native installer) and log in once: run `claude`, then `/login`.
2. Install the helper and units, with your data folder (the host side of `/data`)
   in place of `/path/to/wren-data` in `wren-claude.service` and `wren-claude.path`:

   ```sh
   install -m 755 wren-claude-helper ~/.local/bin/
   mkdir -p ~/.config/systemd/user
   cp wren-claude.service wren-claude.path wren-claude.timer ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now wren-claude.path wren-claude.timer
   systemctl --user start wren-claude.service    # first report to Wren
   ```

3. Check: `cat /path/to/wren-data/ads/helper.json` says `"ok": true`, and Wren's
   Status page shows sponsor reads found in transcripts.

If Claude Code logs out, Wren alerts you ("Claude on the server isn't
answering"); log in again and the alert clears within 10 minutes.
