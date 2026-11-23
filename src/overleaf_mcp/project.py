"""The attached project: one Overleaf project, one local working copy.

A server process holds at most one ``Workspace`` at a time. Every file
operation goes through ``Workspace.resolve`` which refuses anything outside the
working copy, so nothing but the attached project is reachable.

Claude Code starts a separate server process per session, so each session has
its own attachment and its own working copy. Claude Desktop starts one process
for all chats, so there the attachment is shared.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import ConfigError, ProjectEntry, Registry, Settings, load_token, parse_project_id
from .git import Git, GitError, _classify

TEXT_EXTENSIONS = {
    ".tex", ".bib", ".cls", ".sty", ".bst", ".bbx", ".cbx", ".lbx", ".txt", ".md",
    ".csv", ".tsv", ".dat", ".json", ".yaml", ".yml", ".xml", ".svg", ".tikz", ".pgf",
    ".lua", ".py", ".r", ".m", ".latexmkrc", ".ist", ".def", ".cfg", ".clo", ".dtx", ".ins",
}
MAX_READ_BYTES = 2_000_000


class ToolError(Exception):
    """An error reported to the model as {ok: false, error, message, remediation}."""

    def __init__(self, code: str, message: str, remediation: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # git marks pack files read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=onerror)


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


@dataclass
class Workspace:
    entry: ProjectEntry
    root: Path
    git: Git
    settings: Settings
    last_sync: float = 0.0

    # ------------------------------------------------------------------ paths

    def resolve(self, rel: str, *, allow_root: bool = False) -> Path:
        """Map a project-relative path to a real path inside the working copy."""
        rel = (rel or "").strip().replace("\\", "/")
        if rel in ("", ".", "/"):
            if allow_root:
                return self.root
            raise ToolError("BAD_PATH", "A file path is required.")
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or rel.startswith("~"):
            raise ToolError("BAD_PATH", f"{rel!r} is absolute. Use a path relative to the project root, e.g. 'chapters/intro.tex'.")
        parts = PurePosixPath(rel).parts
        if any(p == ".." for p in parts):
            raise ToolError("BAD_PATH", f"{rel!r} leaves the project. '..' is not allowed.")
        if any(p.lower() == ".git" for p in parts):
            raise ToolError("BAD_PATH", "The .git directory is not part of the project.")
        full = (self.root / Path(*parts)).resolve()
        root = self.root.resolve()
        if full != root and root not in full.parents:
            raise ToolError("BAD_PATH", f"{rel!r} resolves outside the project.")
        return full

    def rel(self, full: Path) -> str:
        return full.resolve().relative_to(self.root.resolve()).as_posix()

    # ------------------------------------------------------------------ git state

    def unpushed(self) -> int:
        r = self.git.run(["rev-list", "--count", "@{u}..HEAD"], cwd=self.root, check=False)
        return int(r.stdout.strip() or 0) if r.returncode == 0 else 0

    def head(self) -> str:
        return self.git.run(["rev-parse", "--short", "HEAD"], cwd=self.root).stdout.strip()

    def sync(self, force: bool = False) -> dict:
        """Pull Overleaf's latest changes (rebasing any unpushed local commits)."""
        if not force and time.monotonic() - self.last_sync < self.settings.sync_interval:
            return {"pulled": False, "reason": "recently synced"}
        before = self.head()
        r = self.git.run(["pull", "--rebase", "--quiet", "origin"], cwd=self.root, check=False)
        if r.returncode != 0:
            conflicted = self._conflicted_files()
            if conflicted:
                self.git.run(["rebase", "--abort"], cwd=self.root, check=False)
                raise ToolError(
                    "CONFLICT",
                    f"Unpushed local commits conflict with changes made in Overleaf: {', '.join(conflicted)}.",
                    "Call discard_unpushed to drop the local commits, re-read the files and redo the edits.",
                )
            try:
                self.git.run(["pull", "--rebase", "--quiet", "origin"], cwd=self.root)
            except GitError as e:
                raise ToolError(e.code, e.message, e.remediation) from e
        self.last_sync = time.monotonic()
        after = self.head()
        changed: list[str] = []
        if before != after:
            d = self.git.run(["diff", "--name-only", before, after], cwd=self.root, check=False)
            changed = [l for l in d.stdout.splitlines() if l]
        return {"pulled": True, "head": after, "changed_files": changed}

    def _conflicted_files(self) -> list[str]:
        r = self.git.run(["diff", "--name-only", "--diff-filter=U"], cwd=self.root, check=False)
        return [l for l in r.stdout.splitlines() if l]

    def commit_and_push(self, paths: list[str], message: str) -> dict:
        self.git.run(["add", "-A", "--", *paths], cwd=self.root)
        staged = self.git.run(["diff", "--cached", "--quiet"], cwd=self.root, check=False)
        if staged.returncode == 0:
            return {"committed": False, "pushed": False, "note": "no change to commit"}
        self.git.run(["commit", "--quiet", "-m", message], cwd=self.root)
        out = {"committed": True, "commit": self.head(), "pushed": False}
        if self.settings.auto_push:
            out.update(self.push(drop_on_conflict=True))
        else:
            out["unpushed_commits"] = self.unpushed()
            out["note"] = "auto-push is off; call push_changes to send to Overleaf"
        return out

    def push(self, drop_on_conflict: bool = False) -> dict:
        for attempt in range(3):
            r = self.git.run(["push", "--quiet", "origin", "HEAD"], cwd=self.root, check=False)
            if r.returncode == 0:
                self.last_sync = time.monotonic()
                return {"pushed": True, "commit": self.head()}
            err = r.stderr.lower()
            if not ("rejected" in err or "fetch first" in err or "non-fast-forward" in err):
                code, remedy = _classify(r.stderr)
                raise ToolError(code, f"Push to Overleaf failed: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'unknown error'}",
                                remedy or "Your change is committed locally; call push_changes to retry.")
            # Someone edited in Overleaf meanwhile: rebase on top and try again.
            pull = self.git.run(["pull", "--rebase", "--quiet", "origin"], cwd=self.root, check=False)
            if pull.returncode != 0:
                conflicted = self._conflicted_files()
                self.git.run(["rebase", "--abort"], cwd=self.root, check=False)
                if drop_on_conflict:
                    # Undo our commit so the working copy matches Overleaf again.
                    self.git.run(["reset", "--hard", "--quiet", "@{u}"], cwd=self.root, check=False)
                    raise ToolError(
                        "CONFLICT",
                        f"{', '.join(conflicted) or 'The file'} was changed in Overleaf at the same time. "
                        "Your edit was NOT applied.",
                        "Re-read the file to see the current text, then redo the edit.",
                    )
                raise ToolError(
                    "CONFLICT",
                    f"Local commits conflict with Overleaf changes in: {', '.join(conflicted)}.",
                    "Call discard_unpushed to drop local commits, then re-read and redo the edits.",
                )
        raise ToolError("PUSH_REJECTED", "Overleaf kept rejecting the push.", "Call push_changes to retry.")


class Session:
    """Holds the (at most one) attached project for this server process."""

    def __init__(self, settings: Settings | None = None, registry: Registry | None = None):
        self.settings = settings or Settings.from_env()
        self.registry = registry or Registry(self.settings.home / "projects.json")
        self.ws: Workspace | None = None
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ attach

    def _lookup(self, ref: str) -> ProjectEntry:
        try:
            entry = self.registry.resolve(ref)
        except ConfigError as e:
            raise ToolError("CONFIG", str(e), "Fix or delete the registry file.") from e
        if entry:
            return entry
        pid = parse_project_id(ref)
        if pid and self.settings.allow_unregistered:
            return ProjectEntry(alias=pid, id=pid)
        known = ", ".join(sorted(self.registry.load())) or "none"
        raise ToolError(
            "NOT_REGISTERED",
            f"{ref!r} is not a registered project. Registered: {known}.",
            "Only projects the user has registered can be attached. Ask the user to run "
            "`overleaf-mcp add <alias> <overleaf-project-url>` in a terminal.",
        )

    def attach(self, ref: str | None) -> Workspace:
        with self.lock:
            pinned = self.settings.pinned
            if pinned:
                pinned_entry = self._lookup_pinned(pinned)
                if ref and self._lookup_quiet(ref) != pinned_entry.id:
                    raise ToolError(
                        "PINNED",
                        f"This server is locked to project '{pinned_entry.alias}'. It cannot attach {ref!r}.",
                        "The lock comes from OVERLEAF_PROJECT in the MCP server config; change it there.",
                    )
                entry = pinned_entry
            else:
                if not ref:
                    raise ToolError("NO_PROJECT", "Name a project to attach.", "Call list_projects to see the registered ones.")
                entry = self._lookup(ref)

            if self.ws and self.ws.entry.id == entry.id:
                return self.ws
            if self.ws:
                self.detach()

            token = load_token()
            if not token and self.settings.git_host.startswith("http"):
                raise ToolError("NO_TOKEN", "No Overleaf Git token is configured.",
                                "Ask the user to run `overleaf-mcp login` in a terminal.")
            git = Git(token)
            work = self.settings.home / "work"
            work.mkdir(parents=True, exist_ok=True)
            root = work / f"{entry.alias}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
            url = f"{self.settings.git_host}/{entry.id}"
            # Repo config is passed to clone (-c) so it applies before checkout.
            # A global core.autocrlf=true (the Git for Windows default) would
            # otherwise rewrite line endings and every file would look modified.
            repo_config = (
                ("user.name", self.settings.commit_name),
                ("user.email", self.settings.commit_email),
                ("core.autocrlf", "false"),
                ("core.safecrlf", "false"),
                ("core.quotepath", "false"),
                ("pull.rebase", "true"),
            )
            cfg_args = [a for k, v in repo_config for a in ("-c", f"{k}={v}")]
            try:
                git.run(["clone", "--quiet", "--origin", "origin", *cfg_args, url, str(root)])
            except GitError as e:
                if root.exists():
                    _rmtree(root)
                raise ToolError(e.code, e.message, e.remediation) from e
            (root / ".git" / "overleaf-mcp-owner").write_text(str(os.getpid()), encoding="utf-8")
            self.ws = Workspace(entry=entry, root=root, git=git, settings=self.settings, last_sync=time.monotonic())
            return self.ws

    def _lookup_quiet(self, ref: str) -> str | None:
        try:
            return self._lookup(ref).id
        except ToolError:
            return parse_project_id(ref)

    def _lookup_pinned(self, pinned: str) -> ProjectEntry:
        entry = self.registry.resolve(pinned)
        if entry:
            return entry
        pid = parse_project_id(pinned)
        if pid:  # pinning in the server config is itself the user's explicit grant
            return ProjectEntry(alias=pid, id=pid)
        raise ToolError("CONFIG", f"OVERLEAF_PROJECT={pinned!r} is neither a registered alias nor a project id.",
                        "Fix OVERLEAF_PROJECT in the MCP server config.")

    def detach(self) -> dict:
        with self.lock:
            if not self.ws:
                return {"detached": False, "note": "nothing was attached"}
            ws, self.ws = self.ws, None
            pending = ws.unpushed()
            dirty = bool(ws.git.run(["status", "--porcelain"], cwd=ws.root, check=False).stdout.strip())
            if pending or dirty:
                return {"detached": True, "project": ws.entry.alias, "kept_working_copy": str(ws.root),
                        "warning": f"{pending} unpushed commit(s) / uncommitted changes were left in the working copy."}
            _rmtree(ws.root)
            return {"detached": True, "project": ws.entry.alias}

    def require(self) -> Workspace:
        """The attached workspace, auto-attaching the pinned project if there is one."""
        if self.ws:
            return self.ws
        if self.settings.pinned:
            return self.attach(None)
        raise ToolError(
            "NOT_ATTACHED",
            "No Overleaf project is attached to this session.",
            "Call list_projects, then attach_project with the alias the user wants.",
        )


def prune_stale_work_dirs(home: Path) -> list[str]:
    """Remove working copies left by server processes that no longer exist."""
    removed: list[str] = []
    work = home / "work"
    if not work.exists():
        return removed
    for d in work.iterdir():
        owner = d / ".git" / "overleaf-mcp-owner"
        try:
            pid = int(owner.read_text().strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            continue
        g = Git(None)
        dirty = g.run(["status", "--porcelain"], cwd=d, check=False).stdout.strip()
        ahead = g.run(["rev-list", "--count", "@{u}..HEAD"], cwd=d, check=False).stdout.strip()
        if dirty or (ahead and ahead != "0"):
            continue  # holds work that never reached Overleaf; leave it for the user
        try:
            _rmtree(d)
            removed.append(d.name)
        except OSError:
            pass
    return removed


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
