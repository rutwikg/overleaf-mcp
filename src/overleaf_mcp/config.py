"""Settings, the project registry and the Git token.

Everything lives under ``~/.overleaf-mcp`` (override with ``OVERLEAF_MCP_HOME``).
It is deliberately not under AppData: the Claude desktop app is MSIX-packaged on
Windows, and AppData is redirected for it and every process it starts, so a CLI
run from a normal terminal and a server started by the app would otherwise see
different files.

Layout::

    ~/.overleaf-mcp/
      token            Overleaf Git authentication token (one line)
      projects.json    registry: {"thesis": {"id": "<24 hex>", "note": "..."}}
      work/            per-session working copies (cleaned up on exit)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_ID_IN_TEXT_RE = re.compile(r"(?:/project/|git\.overleaf\.com/|^)([0-9a-f]{24})(?:[/?#.]|$)")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

DEFAULT_GIT_HOST = "https://git.overleaf.com"


class ConfigError(Exception):
    """A problem with local configuration the user has to fix."""


def home_dir() -> Path:
    return Path(os.environ.get("OVERLEAF_MCP_HOME") or Path.home() / ".overleaf-mcp")


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_project_id(text: str) -> str | None:
    """Pull a 24-hex Overleaf project id out of an id, project URL or git URL."""
    text = text.strip()
    if PROJECT_ID_RE.match(text):
        return text
    m = _ID_IN_TEXT_RE.search(text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- registry


@dataclass
class ProjectEntry:
    alias: str
    id: str
    note: str = ""


class Registry:
    """The list of projects the user has allowed this server to touch.

    Only the user edits this (via the ``overleaf-mcp`` CLI); the server only
    reads it, so a model cannot widen its own access through the MCP tools.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or home_dir() / "projects.json"

    def load(self) -> dict[str, ProjectEntry]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{self.path} is not valid JSON: {exc}") from exc
        out: dict[str, ProjectEntry] = {}
        for alias, item in raw.items():
            if isinstance(item, str):
                item = {"id": item}
            out[alias] = ProjectEntry(alias=alias, id=item["id"], note=item.get("note", ""))
        return out

    def save(self, entries: dict[str, ProjectEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {e.alias: {"id": e.id, **({"note": e.note} if e.note else {})} for e in entries.values()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def add(self, alias: str, url_or_id: str, note: str = "") -> ProjectEntry:
        if not ALIAS_RE.match(alias):
            raise ConfigError("Alias must be 1-64 characters: letters, digits, '.', '_' or '-'.")
        pid = parse_project_id(url_or_id)
        if not pid:
            raise ConfigError(f"Could not find an Overleaf project id in {url_or_id!r}.")
        entries = self.load()
        entries[alias] = ProjectEntry(alias=alias, id=pid, note=note)
        self.save(entries)
        return entries[alias]

    def remove(self, alias: str) -> bool:
        entries = self.load()
        if alias not in entries:
            return False
        del entries[alias]
        self.save(entries)
        return True

    def rename(self, old: str, new: str) -> ProjectEntry:
        if not ALIAS_RE.match(new):
            raise ConfigError("Alias must be 1-64 characters: letters, digits, '.', '_' or '-'.")
        entries = self.load()
        if old not in entries:
            raise ConfigError(f"{old!r} is not registered. Registered: {', '.join(sorted(entries)) or 'none'}.")
        if new in entries and new != old:
            raise ConfigError(f"{new!r} is already in use.")
        entry = entries.pop(old)
        entry.alias = new
        entries[new] = entry
        self.save(entries)
        return entry

    def resolve(self, ref: str) -> ProjectEntry | None:
        """Find a registered project by alias, id or URL. None if not registered."""
        entries = self.load()
        if ref in entries:
            return entries[ref]
        lowered = {k.lower(): v for k, v in entries.items()}
        if ref.lower() in lowered:
            return lowered[ref.lower()]
        pid = parse_project_id(ref)
        if pid:
            for e in entries.values():
                if e.id == pid:
                    return e
        return None


# --------------------------------------------------------------------------- token


def token_path() -> Path:
    return home_dir() / "token"


def load_token() -> str | None:
    tok = os.environ.get("OVERLEAF_GIT_TOKEN", "").strip()
    if tok:
        return tok
    p = token_path()
    if p.exists():
        tok = p.read_text(encoding="utf-8").strip()
        return tok or None
    return None


def save_token(token: str) -> Path:
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)  # meaningful on POSIX; on Windows the profile ACL already restricts it
    except OSError:
        pass
    return p


# --------------------------------------------------------------------------- settings


@dataclass
class Settings:
    git_host: str = DEFAULT_GIT_HOST
    # If set, the server is locked to this one project (alias, id or URL).
    pinned: str | None = None
    # Allow attaching by raw id/URL without registering first.
    allow_unregistered: bool = False
    # Push to Overleaf after every write. If False, writes are committed
    # locally and go up on push_changes.
    auto_push: bool = True
    # Pull from Overleaf before an operation if the last pull is older than this.
    sync_interval: float = 15.0
    commit_name: str = "Claude (overleaf-mcp)"
    commit_email: str = "overleaf-mcp@localhost"
    home: Path = field(default_factory=home_dir)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            git_host=(env.get("OVERLEAF_GIT_HOST") or DEFAULT_GIT_HOST).rstrip("/"),
            pinned=(env.get("OVERLEAF_PROJECT") or "").strip() or None,
            allow_unregistered=_truthy(env.get("OVERLEAF_ALLOW_UNREGISTERED"), False),
            auto_push=_truthy(env.get("OVERLEAF_AUTO_PUSH"), True),
            sync_interval=float(env.get("OVERLEAF_SYNC_INTERVAL") or 15),
            commit_name=env.get("OVERLEAF_COMMIT_NAME") or cls.commit_name,
            commit_email=env.get("OVERLEAF_COMMIT_EMAIL") or cls.commit_email,
            home=home_dir(),
        )
