import argparse


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

    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
