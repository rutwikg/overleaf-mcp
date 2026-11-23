"""Command line: run the server, store the token, register projects.

    overleaf-mcp                    run the MCP server on stdio (what clients launch)
    overleaf-mcp login              store your Overleaf Git token
    overleaf-mcp add ALIAS URL      allow a project (alias is what you tell Claude)
    overleaf-mcp rename OLD NEW     change a project's alias
    overleaf-mcp remove ALIAS
    overleaf-mcp list
    overleaf-mcp check [ALIAS]      verify the token can reach registered projects
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .config import ConfigError, Registry, Settings, load_token, save_token, token_path
from .git import Git, GitError


def _check(alias: str | None) -> int:
    settings = Settings.from_env()
    token = load_token()
    if not token:
        print("No token. Run `overleaf-mcp login` first.", file=sys.stderr)
        return 1
    reg = Registry(settings.home / "projects.json")
    entries = reg.load()
    targets = [entries[alias]] if alias else list(entries.values())
    if alias and alias not in entries:
        print(f"{alias} is not registered.", file=sys.stderr)
        return 1
    if not targets:
        print("No projects registered. Run `overleaf-mcp add ALIAS URL`.")
        return 1
    git = Git(token, timeout=60)
    bad = 0
    for e in targets:
        try:
            git.run(["ls-remote", "--heads", f"{settings.git_host}/{e.id}"])
            print(f"  ok    {e.alias}  ({e.id})")
        except GitError as err:
            bad += 1
            print(f"  FAIL  {e.alias}  ({e.id}): {err.code} {err.message}")
            if err.remediation:
                print(f"        {err.remediation}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="overleaf-mcp", description="MCP server for one Overleaf project at a time.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    lg = sub.add_parser("login", help="store your Overleaf Git token")
    lg.add_argument("--token", help="token value (otherwise prompted, hidden)")
    a = sub.add_parser("add", help="register a project")
    a.add_argument("alias")
    a.add_argument("url", help="Overleaf project URL, git URL or 24-character id")
    a.add_argument("--note", default="")
    rn = sub.add_parser("rename", help="change a project's alias")
    rn.add_argument("old")
    rn.add_argument("new")
    r = sub.add_parser("remove", help="unregister a project")
    r.add_argument("alias")
    sub.add_parser("list", help="list registered projects")
    c = sub.add_parser("check", help="verify the token can reach registered projects")
    c.add_argument("alias", nargs="?")
    args = p.parse_args(argv)

    if args.cmd in (None, "serve"):
        from .server import main as serve

        serve()
        return 0

    settings = Settings.from_env()
    reg = Registry(settings.home / "projects.json")
    try:
        if args.cmd == "login":
            print("Create a token in Overleaf: Account Settings > Git integration > Generate token.")
            tok = args.token or getpass.getpass("Overleaf Git token (input hidden): ")
            if not tok.strip():
                print("No token entered.", file=sys.stderr)
                return 1
            path = save_token(tok)
            print(f"Saved to {path}")
            if reg.load():
                return _check(None)
            return 0
        if args.cmd == "add":
            e = reg.add(args.alias, args.url, args.note)
            print(f"Registered {e.alias} -> {e.id}")
            if load_token():
                return _check(e.alias)
            print("No token stored yet; run `overleaf-mcp login`.")
            return 0
        if args.cmd == "rename":
            e = reg.rename(args.old, args.new)
            print(f"Renamed {args.old} -> {e.alias} ({e.id})")
            print("Sessions already attached keep working. Update any OVERLEAF_PROJECT that used the old name.")
            return 0
        if args.cmd == "remove":
            ok = reg.remove(args.alias)
            print("Removed." if ok else f"{args.alias} was not registered.")
            return 0 if ok else 1
        if args.cmd == "list":
            entries = reg.load()
            if not entries:
                print("No projects registered.")
            for e in entries.values():
                print(f"{e.alias:20} {e.id}  https://www.overleaf.com/project/{e.id}" + (f"  # {e.note}" if e.note else ""))
            print(f"\ntoken: {'set' if load_token() else 'NOT SET'} ({token_path()})")
            return 0
        if args.cmd == "check":
            return _check(args.alias)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
