from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .engine import ContractError, MAX_HTML, MAX_RULES, extract, load_rules
from .remote import RemoteSource
from .server import ROOT, Server, Source


def read_text(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ContractError("file_limit", "An input file exceeds its documented size limit.")
        return data.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ContractError("file", "An input file is unavailable or is not UTF-8 text.") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Turn authorized static HTML into a checked JSON API.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "serve"):
        command = sub.add_parser(name)
        command.add_argument("--rules", type=Path, default=ROOT / "data" / "rules.json")
        command.add_argument("--source", type=Path)
        if name == "serve":
            command.add_argument("--port", type=int, default=8788)
            command.add_argument("--remote-url")
            command.add_argument("--allow-host")
            command.add_argument("--refresh-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    try:
        rules = load_rules(read_text(args.rules, MAX_RULES))
        demo_html = read_text(ROOT / "data" / "catalog.html", MAX_HTML)
        demo_rules = load_rules(read_text(ROOT / "data" / "rules.json", MAX_RULES))
        if args.command == "preview":
            html = read_text(args.source or ROOT / "data" / "catalog.html", MAX_HTML)
            print(json.dumps(extract(html, rules, source_kind="local"), ensure_ascii=False, indent=2))
            return 0
        if not 1 <= args.port <= 65535:
            raise ContractError("port", "Port must be from 1 to 65535.")
        if args.source and args.remote_url:
            raise ContractError("source", "Choose either a local source or a preconfigured remote source.")
        if args.allow_host and not args.remote_url:
            raise ContractError("source", "--allow-host requires --remote-url.")
        remote = RemoteSource(args.remote_url, args.allow_host, args.refresh_seconds) if args.remote_url else None
        source = Source(rules, args.source or ROOT / "data" / "catalog.html", remote)
        server = Server(("127.0.0.1", args.port), source, demo_html, demo_rules)
        print(f"Site Contract API: http://127.0.0.1:{args.port}/")
        print("Fixed endpoint: /api/data. " + ("Remote reads explicitly enabled; requests are not logged." if remote else "Offline mode. No remote requests."))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except ContractError as exc:
        print(json.dumps(exc.as_dict()), file=sys.stderr)
        return 1
    except OSError:
        print("The local server could not start. Check whether the selected port is available.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
