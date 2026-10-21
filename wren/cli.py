import argparse
import logging
import time


def _ago(ts: int | None) -> str:
    if not ts:
        return "never"
    mins = int((time.time() - ts) // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    if mins < 60 * 24:
        return f"{mins // 60}h ago"
    return f"{mins // (60 * 24)}d ago"


def _ads_check(parser, video_id: str) -> None:
    """Transcribe an episode and ask Claude for its ad reads (compared with
    SponsorBlock's marks) and its chapters: a dry run to see how well it works on a show."""
    import time

    from . import ads, chapters, db, notes, sponsors, transcribe
    from .config import MEDIA_DIR

    if not transcribe.available():
        parser.exit(1, "wren: Whisper (faster-whisper) isn't installed\n")
    if not ads.available():
        parser.exit(1, "wren: set ANTHROPIC_API_KEY to find ad reads\n")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM episodes WHERE video_id = ? ORDER BY mode LIMIT 1", (video_id,)).fetchone()
    if not row:
        parser.exit(1, f"wren: no downloaded episode {video_id}\n")
    ep = dict(row)
    clock = lambda t: f"{int(t) // 60}:{int(t) % 60:02d}"  # noqa: E731
    print(f"{ep['title']} ({clock(ep['duration'] or 0)})")
    transcript = transcribe.load(video_id)
    if transcript:
        print("using the transcript saved when it was processed (times in the original)")
    else:
        if ep["sponsor_state"] == "done" and ep["sponsor_removed"]:
            print("note: this copy has already been cut, so times are in the cut file")
        transcript = transcribe.transcribe(MEDIA_DIR / ep["file"])
        rate = (ep["duration"] or 0) / transcript["seconds"] if transcript["seconds"] else 0
        print(f"transcribed in {transcript['seconds']:.0f}s ({rate:.0f}x real time)")
    t0 = time.time()
    print(f"asking Claude ({ads.backend()})")
    ids = (video_id, chapters.request_id(video_id))
    for rid in ids:
        ads.forget(rid)  # an answer left from an earlier run
    answer = chaptered = None
    while answer is None or chaptered is None:  # both questions out at once
        try:
            answer = answer or ads.ask_claude(transcript, ep["title"], video_id, ep.get("description"))
        except ads.Pending:
            pass
        try:
            chaptered = chaptered or chapters.ask(transcript, ep["title"], video_id)
        except ads.Pending:
            pass
        if answer is None or chaptered is None:
            if time.time() - t0 > 15 * 60:
                parser.exit(1, "wren: no answer from the helper on the host in 15 minutes\n")
            time.sleep(5)
    for rid in ids:
        ads.forget(rid)
    print(f"Claude answered in {time.time() - t0:.0f}s")
    cuts, note = ads.choose(answer.ads, transcript, ep["duration"])
    for a in answer.ads:
        print(f"  {clock(a.start)}-{clock(a.end)}  {a.advertiser}  [{a.kind}, {a.confidence}]"
              f"  \"{a.first_words}\" ... \"{a.last_words}\"")
    print("would cut: " + (", ".join(f"{c['start']:.1f}-{c['end']:.1f}" for c in cuts) or "nothing"))
    if note:
        print(note)
    lines = notes.lines(ep.get("description"))
    for n in answer.description_lines:
        if 0 < n <= len(lines):
            print(f"  description line {n}: {lines[n - 1][:100]}")
    try:
        marks = sponsors.merge(sponsors.lookup(video_id), ep["duration"])
        print("SponsorBlock: " + (", ".join(f"{clock(a)}-{clock(b)}" for a, b in marks) or "no marks"))
    except Exception as e:
        print(f"SponsorBlock: unreachable ({e})")
    generated = chapters.place(chaptered, transcript)
    creator = [] if sponsors._is_podcast(ep) else chapters.from_description(ep.get("description"), ep["duration"])
    chosen, source = chapters.choose(creator, generated)
    print(f"chapters: {len(chosen)}, {source or 'none'}"
          f"{' (the creator listed some, but they were set aside)' if creator and source != 'creator' else ''}")
    for c in chosen:  # on the original timeline, like the cuts above
        print(f"  {clock(c['start']):>7}  {c['title']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="wren")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="download a single video/episode")
    p_add.add_argument("url")
    p_add.add_argument("--mode", choices=["audio", "video"], default="audio")

    p_serve = sub.add_parser("serve", help="run the API + PWA server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("list", help="list downloaded episodes")

    p_subscribe = sub.add_parser("subscribe", help="follow a YouTube channel or playlist")
    p_subscribe.add_argument("url")
    p_subscribe.add_argument("--mode", choices=["audio", "video"], default="audio")
    p_subscribe.add_argument("--keep", type=int, default=10,
                             help="episodes to keep (0 keeps everything; default 10)")
    p_subscribe.add_argument("--backfill", type=int, default=3,
                             help="how many existing episodes to download now (default 3)")
    p_subscribe.add_argument("--min-duration", type=int, default=300,
                             help="skip episodes shorter than this many seconds (default 300)")
    p_subscribe.add_argument("--title-filter", help="only download episodes whose title matches this regex")
    p_subscribe.add_argument("--no-sync", action="store_true",
                             help="just subscribe; download on the next sync")

    sub.add_parser("shows", help="list subscriptions")

    p_unsub = sub.add_parser("unsubscribe", help="stop following a show (keeps its episodes)")
    p_unsub.add_argument("show_id", type=int)

    p_check = sub.add_parser("ads-check", help="find the ad reads in a downloaded episode, without cutting anything")
    p_check.add_argument("video_id")

    p_sync = sub.add_parser("sync", help="check subscriptions and download new episodes")
    p_sync.add_argument("show_id", type=int, nargs="?")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "add":
        from .downloader import download

        ep = download(args.url, args.mode)
        print(f"Saved: {ep['title']} -> {ep['file']}")
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("wren.server:app", host=args.host, port=args.port)
    elif args.cmd == "list":
        from .db import list_episodes

        for ep in list_episodes():
            print(f"[{ep['mode']:5}] {ep['title']}  ({ep['file']})")
    elif args.cmd == "subscribe":
        from . import subscriptions

        try:
            show = subscriptions.subscribe(
                args.url, args.mode, keep_last=args.keep, backfill=args.backfill,
                min_duration=args.min_duration, title_filter=args.title_filter)
        except ValueError as e:
            parser.exit(1, f"wren: {e}\n")
        print(f"Subscribed: [{show['id']}] {show['name']}")
        if not args.no_sync:
            totals = subscriptions.sync(show["id"])
            print(f"Downloaded {totals['downloaded']} episode(s)")
    elif args.cmd == "shows":
        from . import subscriptions

        shows = subscriptions.list_shows()
        if not shows:
            print("No subscriptions. Add one with: wren subscribe <channel or playlist URL>")
        for s in shows:
            keep = "all" if s["keep_last"] <= 0 else s["keep_last"]
            status = [f"{s['episodes']} episodes", f"keep {keep}", s["mode"],
                      f"checked {_ago(s['last_checked'])}"]
            if s["queued"]:
                status.append(f"{s['queued']} queued")
            if s["failed"]:
                status.append(f"{s['failed']} failed")
            print(f"[{s['id']}] {s['name']}  ({', '.join(status)})")
            if s["last_error"]:
                print(f"    error: {s['last_error']}")
    elif args.cmd == "unsubscribe":
        from . import subscriptions

        show = subscriptions.get_show(args.show_id)
        if not show:
            parser.exit(1, f"wren: no show with id {args.show_id}\n")
        subscriptions.unsubscribe(args.show_id)
        print(f"Unsubscribed from {show['name']} (its episodes stay in the library)")
    elif args.cmd == "ads-check":
        _ads_check(parser, args.video_id)
    elif args.cmd == "sync":
        from . import subscriptions

        totals = subscriptions.sync(args.show_id)
        print(f"Checked {totals['shows']} show(s): {totals['new']} new, "
              f"{totals['downloaded']} downloaded, {totals['removed']} removed")


if __name__ == "__main__":
    main()
