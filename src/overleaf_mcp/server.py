"""MCP server exposing one attached Overleaf project to Claude."""

from __future__ import annotations

import atexit
import fnmatch
import functools
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .git import GitError
from .project import (
    MAX_READ_BYTES,
    TEXT_EXTENSIONS,
    Session,
    ToolError,
    Workspace,
    _is_binary,
    prune_stale_work_dirs,
)

log = logging.getLogger("overleaf_mcp")

INSTRUCTIONS = """\
Edits one Overleaf LaTeX project at a time, through Overleaf's Git integration.

Workflow
  1. If no project is attached, call list_projects and attach_project with the
     alias the user names. Only projects the user has registered can be attached;
     you cannot add new ones yourself.
  2. Orient with list_files and outline, then read_file before editing.
  3. Change text with edit_file (exact find/replace, like a code editor) or
     apply_edits for several changes to one file in a single commit. Use
     write_file only for new files or full rewrites.
  4. Every write is committed and pushed straight to Overleaf, so it appears in
     the user's Overleaf editor within seconds. The commit message is shown in
     Overleaf's history; make it say what changed.

Paths are relative to the project root, with forward slashes ('chapters/intro.tex').
Every result is JSON with an 'ok' flag; failures carry 'error' and 'remediation'.
A CONFLICT means the user edited the same text in Overleaf at the same moment:
re-read the file and redo the edit. This server does not compile LaTeX.
"""

mcp = MCPServer(name="overleaf", instructions=INSTRUCTIONS)
session = Session()

RO = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def _fail(code: str, message: str, remediation: str = "") -> dict:
    out: dict[str, Any] = {"ok": False, "error": code, "message": message}
    if remediation:
        out["remediation"] = remediation
    return out


def tool(**kwargs):
    """Register a tool that returns {ok: true, ...} or a structured failure."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                with session.lock:
                    result = fn(*a, **kw)
                return {"ok": True, **result}
            except ToolError as e:
                return _fail(e.code, e.message, e.remediation)
            except GitError as e:
                return _fail(e.code, e.message, e.remediation)
            except Exception as e:  # noqa: BLE001 - never crash the stdio loop
                log.exception("tool %s failed", fn.__name__)
                return _fail("INTERNAL", f"{type(e).__name__}: {e}")

        return mcp.tool(structured_output=False, **kwargs)(wrapper)

    return deco


def _ws(sync: bool = True) -> Workspace:
    ws = session.require()
    if sync:
        ws.sync()
    return ws


def _read_text(ws: Workspace, path: str) -> tuple[Path, str]:
    full = ws.resolve(path)
    if not full.exists():
        raise ToolError("NOT_FOUND", f"{path} does not exist.", "Call list_files to see the project's files.")
    if full.is_dir():
        raise ToolError("IS_DIRECTORY", f"{path} is a folder.", "Call list_files with this path.")
    data = full.read_bytes()
    if len(data) > MAX_READ_BYTES:
        raise ToolError("TOO_LARGE", f"{path} is {len(data)} bytes.", "Read it in parts with start_line/end_line.")
    if _is_binary(data):
        raise ToolError("BINARY", f"{path} is a binary file ({len(data)} bytes).", "Binary files cannot be read as text.")
    return full, data.decode("utf-8", errors="replace")


def _write_bytes(full: Path, data: bytes) -> None:
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)


def _msg(message: str | None, default: str) -> str:
    return (message or "").strip() or default


# =========================================================================== projects


@tool(annotations=RO)
def list_projects() -> dict:
    """List the Overleaf projects the user has registered for use with Claude, and which one is attached."""
    entries = session.registry.load()
    attached = session.ws.entry.alias if session.ws else None
    out: dict[str, Any] = {
        "projects": [{"alias": e.alias, "id": e.id, **({"note": e.note} if e.note else {})} for e in entries.values()],
        "attached": attached,
    }
    if session.settings.pinned:
        out["pinned"] = session.settings.pinned
        out["note"] = "This server is locked to the pinned project; attach_project cannot switch."
    if not entries and not session.settings.pinned:
        out["remediation"] = "No projects registered. Ask the user to run `overleaf-mcp add <alias> <project-url>` in a terminal."
    return out


@tool(annotations=WRITE)
def attach_project(
    project: Annotated[str, Field(description="Registered alias (from list_projects), or the project's Overleaf URL or id. Omit when the server is pinned to one project.")] = "",
) -> dict:
    """Attach an Overleaf project to this session. All other tools then act on this project only.

    Clones the project locally, which takes a few seconds. Attaching a different
    project detaches the current one first.
    """
    ws = session.attach(project or None)
    return {"attached": ws.entry.alias, "project_id": ws.entry.id, "files": _file_list(ws, ws.root)[:200],
            "main_file": _guess_main(ws)}


@tool(annotations=WRITE)
def detach_project() -> dict:
    """Detach the current project. Afterwards no project is reachable until attach_project is called again."""
    if session.settings.pinned:
        raise ToolError("PINNED", "This server is locked to one project and cannot detach.")
    return session.detach()


@tool(annotations=RO)
def project_status() -> dict:
    """Show which project is attached, whether it is in sync with Overleaf, and the push mode."""
    if not session.ws and not session.settings.pinned:
        return {"attached": None, "pinned": None}
    ws = session.require()
    return {
        "attached": ws.entry.alias,
        "project_id": ws.entry.id,
        "pinned": bool(session.settings.pinned),
        "overleaf_url": f"https://www.overleaf.com/project/{ws.entry.id}",
        "head": ws.head(),
        "unpushed_commits": ws.unpushed(),
        "auto_push": session.settings.auto_push,
        "working_copy": str(ws.root),
    }


# =========================================================================== reading


def _file_list(ws: Workspace, base: Path) -> list[str]:
    files = []
    for p in sorted(base.rglob("*")):
        if ".git" in p.relative_to(ws.root).parts or not p.is_file():
            continue
        files.append(ws.rel(p))
    return files


def _guess_main(ws: Workspace) -> str | None:
    candidates = []
    for p in ws.root.rglob("*.tex"):
        if ".git" in p.relative_to(ws.root).parts:
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")[:5000]
        except OSError:
            continue
        if re.search(r"^\s*\\documentclass", head, re.M):
            candidates.append(ws.rel(p))
    if not candidates:
        return None
    for preferred in ("main.tex", "thesis.tex", "paper.tex"):
        if preferred in candidates:
            return preferred
    return sorted(candidates, key=lambda c: (c.count("/"), c))[0]


@tool(annotations=RO)
def list_files(
    path: Annotated[str, Field(description="Folder to list, relative to the project root. Empty for the whole project.")] = "",
) -> dict:
    """List files in the attached project (recursively), with sizes."""
    ws = _ws()
    base = ws.resolve(path, allow_root=True)
    if not base.exists():
        raise ToolError("NOT_FOUND", f"{path} does not exist.")
    files = _file_list(ws, base) if base.is_dir() else [ws.rel(base)]
    return {
        "project": ws.entry.alias,
        "files": [{"path": f, "bytes": (ws.root / f).stat().st_size} for f in files],
        "main_file": _guess_main(ws),
    }


@tool(annotations=RO)
def read_file(
    path: Annotated[str, Field(description="File path relative to the project root, e.g. 'main.tex'.")],
    start_line: Annotated[int | None, Field(description="First line to return (1-based). Omit for the start.", ge=1)] = None,
    end_line: Annotated[int | None, Field(description="Last line to return (inclusive). Omit for the end.", ge=1)] = None,
    line_numbers: Annotated[bool, Field(description="Prefix each line with its number. Leave off when you will copy text into edit_file.")] = False,
) -> dict:
    """Read a text file from the attached project."""
    ws = _ws()
    _, text = _read_text(ws, path)
    lines = text.splitlines(keepends=True)
    total = len(lines)
    s = (start_line or 1) - 1
    e = end_line or total
    chunk = lines[s:e]
    if line_numbers:
        content = "".join(f"{s + i + 1:>5}| {ln}" for i, ln in enumerate(chunk))
    else:
        content = "".join(chunk)
    return {"path": path, "total_lines": total, "start_line": s + 1, "end_line": min(e, total), "content": content}


@tool(annotations=RO)
def search_project(
    pattern: Annotated[str, Field(description="Text to find. Treated literally unless regex=true.")],
    regex: Annotated[bool, Field(description="Interpret pattern as a Python regular expression.")] = False,
    file_glob: Annotated[str, Field(description="Only search files matching this glob, e.g. '*.tex' or 'chapters/*'.")] = "",
    ignore_case: bool = False,
    max_results: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> dict:
    """Search the text files of the attached project. Returns matching lines with file and line number."""
    ws = _ws()
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as e:
        raise ToolError("BAD_REGEX", str(e))
    hits = []
    for rel in _file_list(ws, ws.root):
        if file_glob and not (fnmatch.fnmatch(rel, file_glob) or fnmatch.fnmatch(Path(rel).name, file_glob)):
            continue
        p = ws.root / rel
        if p.suffix.lower() not in TEXT_EXTENSIONS and p.suffix:
            continue
        data = p.read_bytes()
        if _is_binary(data):
            continue
        for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append({"path": rel, "line": n, "text": line.strip()[:300]})
                if len(hits) >= max_results:
                    return {"matches": hits, "truncated": True}
    return {"matches": hits, "truncated": False}


_SECTION_RE = re.compile(
    r"^(?!\s*%).*?\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{(.*)"
)
_INCLUDE_RE = re.compile(r"^(?!\s*%).*?\\(input|include|subfile)\s*\{([^}]+)\}")
_LEVEL = {"part": 0, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4, "paragraph": 5}


def _balanced(s: str) -> str:
    depth, out = 1, []
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
    return "".join(out).strip()


@tool(annotations=RO)
def outline(
    path: Annotated[str, Field(description="Start from this .tex file and follow \\input/\\include. Omit to start from the detected main file.")] = "",
) -> dict:
    """Show the document structure: sectioning commands in reading order, following \\input and \\include."""
    ws = _ws()
    start = path or _guess_main(ws)
    if not start:
        raise ToolError("NO_MAIN", "No file with \\documentclass found.", "Pass a .tex path explicitly.")
    items: list[dict] = []
    seen: set[str] = set()

    def walk(rel: str, depth: int) -> None:
        if rel in seen or depth > 20:
            return
        seen.add(rel)
        try:
            full = ws.resolve(rel)
        except ToolError:
            return
        if not full.exists() and full.with_suffix(".tex").exists():
            full = full.with_suffix(".tex")
        if not full.is_file():
            return
        relp = ws.rel(full)
        for n, line in enumerate(full.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = _SECTION_RE.match(line)
            if m:
                items.append({"level": _LEVEL[m.group(1)], "kind": m.group(1), "title": _balanced(m.group(2)),
                              "file": relp, "line": n})
                continue
            m = _INCLUDE_RE.match(line)
            if m:
                target = m.group(2).strip()
                if not Path(target).suffix:
                    target += ".tex"
                walk(target, depth + 1)

    walk(start, 0)
    return {"main_file": start, "outline": items, "files_visited": sorted(seen)}


@tool(annotations=RO)
def project_history(
    limit: Annotated[int, Field(ge=1, le=100)] = 15,
    path: Annotated[str, Field(description="Only commits touching this file. Empty for all.")] = "",
) -> dict:
    """Recent versions of the project (as recorded by Overleaf's Git bridge), newest first."""
    ws = _ws()
    args = ["log", f"-n{limit}", "--date=iso", "--pretty=format:%h%x1f%an%x1f%ad%x1f%s"]
    if path:
        args += ["--", ws.rel(ws.resolve(path))]
    out = ws.git.run(args, cwd=ws.root).stdout
    commits = []
    for line in out.splitlines():
        h, a, d, s = (line.split("\x1f") + ["", "", "", ""])[:4]
        commits.append({"commit": h, "author": a, "date": d, "message": s})
    return {"commits": commits}


@tool(annotations=RO)
def show_diff(
    commit: Annotated[str, Field(description="Commit from project_history. The diff shows what that commit changed.")],
    path: Annotated[str, Field(description="Limit to one file.")] = "",
) -> dict:
    """Show what one version of the project changed."""
    ws = _ws(sync=False)
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", commit):
        raise ToolError("BAD_COMMIT", "Pass a commit hash from project_history.")
    args = ["show", "--stat", "--patch", "--format=%h %an %ad%n%s%n", commit]
    if path:
        args += ["--", ws.rel(ws.resolve(path))]
    out = ws.git.run(args, cwd=ws.root).stdout
    return {"diff": out[:100_000], "truncated": len(out) > 100_000}


# =========================================================================== writing


def _match_newlines(text: str, sample: str) -> str:
    if "\r\n" in sample:
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text


def _apply_one(text: str, old: str, new: str, replace_all: bool, path: str) -> tuple[str, int]:
    old = _match_newlines(old, text)
    new = _match_newlines(new, text)
    if old == "":
        raise ToolError("BAD_EDIT", "old_text is empty.", "To add text, include a nearby line in old_text and repeat it in new_text.")
    count = text.count(old)
    if count == 0:
        hint = ""
        first = old.strip().splitlines()[0].strip() if old.strip() else ""
        if first:
            lines = [i + 1 for i, ln in enumerate(text.splitlines()) if first in ln]
            if lines:
                hint = f" Its first line appears at line(s) {lines[:10]}; whitespace or later lines differ."
        raise ToolError("NO_MATCH", f"old_text was not found in {path}.{hint}",
                        "Re-read the file (read_file) and copy old_text exactly, including whitespace.")
    if count > 1 and not replace_all:
        starts, i = [], text.find(old)
        while i != -1:
            starts.append(text.count("\n", 0, i) + 1)
            i = text.find(old, i + 1)
        raise ToolError("AMBIGUOUS", f"old_text occurs {count} times in {path} (lines {starts[:20]}).",
                        "Add surrounding text to make it unique, or set replace_all=true.")
    return text.replace(old, new) if replace_all else text.replace(old, new, 1), count if replace_all else 1


@tool(annotations=WRITE)
def edit_file(
    path: Annotated[str, Field(description="File to edit, relative to the project root.")],
    old_text: Annotated[str, Field(description="Exact text currently in the file, including whitespace and line breaks. Must be unique unless replace_all is true.")],
    new_text: Annotated[str, Field(description="Replacement text.")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence instead of requiring exactly one.")] = False,
    message: Annotated[str, Field(description="Commit message shown in Overleaf's history.")] = "",
) -> dict:
    """Replace exact text in a file of the attached project and push it to Overleaf."""
    ws = _ws()
    full, text = _read_text(ws, path)
    new, n = _apply_one(text, old_text, new_text, replace_all, path)
    if new == text:
        return {"path": path, "replacements": 0, "note": "new_text equals old_text; nothing changed"}
    _write_bytes(full, new.encode("utf-8"))
    res = ws.commit_and_push([ws.rel(full)], _msg(message, f"Edit {path}"))
    return {"path": path, "replacements": n, **res}


class Edit(BaseModel):
    old_text: str = Field(description="Exact text currently in the file.")
    new_text: str = Field(description="Replacement text.")
    replace_all: bool = False


@tool(annotations=WRITE)
def apply_edits(
    path: Annotated[str, Field(description="File to edit, relative to the project root.")],
    edits: Annotated[list[Edit], Field(description="Edits applied in order; each sees the result of the previous one. All succeed or none are applied.")],
    message: Annotated[str, Field(description="Commit message shown in Overleaf's history.")] = "",
) -> dict:
    """Apply several find/replace edits to one file as a single Overleaf change."""
    ws = _ws()
    full, text = _read_text(ws, path)
    original = text
    total = 0
    for i, e in enumerate(edits, 1):
        try:
            text, n = _apply_one(text, e.old_text, e.new_text, e.replace_all, path)
        except ToolError as err:
            raise ToolError(err.code, f"Edit {i} of {len(edits)}: {err.message} No edits were applied.", err.remediation)
        total += n
    if text == original:
        return {"path": path, "replacements": 0, "note": "nothing changed"}
    _write_bytes(full, text.encode("utf-8"))
    res = ws.commit_and_push([ws.rel(full)], _msg(message, f"Edit {path}"))
    return {"path": path, "edits": len(edits), "replacements": total, **res}


@tool(annotations=DESTRUCTIVE)
def write_file(
    path: Annotated[str, Field(description="File to create or overwrite, relative to the project root. Folders are created as needed.")],
    content: Annotated[str, Field(description="The complete new file content.")],
    message: Annotated[str, Field(description="Commit message shown in Overleaf's history.")] = "",
) -> dict:
    """Create a new text file, or replace a file's entire content, and push it to Overleaf.

    Prefer edit_file for changes to existing files: it cannot accidentally drop text.
    """
    ws = _ws()
    full = ws.resolve(path)
    if full.is_dir():
        raise ToolError("IS_DIRECTORY", f"{path} is a folder.")
    existed = full.exists()
    _write_bytes(full, content.encode("utf-8"))
    res = ws.commit_and_push([ws.rel(full)], _msg(message, f"{'Rewrite' if existed else 'Add'} {path}"))
    return {"path": path, "created": not existed, "bytes": len(content.encode("utf-8")), **res}


@tool(annotations=WRITE)
def upload_file(
    local_path: Annotated[str, Field(description="Absolute path of a file on this computer (e.g. a figure PNG or PDF).")],
    path: Annotated[str, Field(description="Destination path in the project, e.g. 'figures/results.png'.")],
    message: Annotated[str, Field(description="Commit message shown in Overleaf's history.")] = "",
) -> dict:
    """Copy a file from this computer (figures, data, .bib) into the attached project and push it."""
    ws = _ws()
    src = Path(local_path).expanduser()
    if not src.is_file():
        raise ToolError("NOT_FOUND", f"{local_path} is not a file on this computer.")
    if src.stat().st_size > 50_000_000:
        raise ToolError("TOO_LARGE", "Overleaf limits uploaded files to 50 MB.")
    full = ws.resolve(path)
    existed = full.exists()
    full.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, full)
    res = ws.commit_and_push([ws.rel(full)], _msg(message, f"{'Replace' if existed else 'Add'} {path}"))
    return {"path": path, "bytes": full.stat().st_size, "replaced": existed, **res}


@tool(annotations=DESTRUCTIVE)
def delete_file(
    path: Annotated[str, Field(description="File to delete, relative to the project root.")],
    message: Annotated[str, Field(description="Commit message shown in Overleaf's history.")] = "",
) -> dict:
    """Delete a file from the attached project. It stays recoverable from Overleaf's history."""
    ws = _ws()
    full = ws.resolve(path)
    if not full.is_file():
        raise ToolError("NOT_FOUND", f"{path} is not a file.")
    rel = ws.rel(full)
    full.unlink()
    res = ws.commit_and_push([rel], _msg(message, f"Delete {path}"))
    return {"deleted": path, **res}


@tool(annotations=WRITE)
def move_file(
    path: Annotated[str, Field(description="Existing file, relative to the project root.")],
    new_path: Annotated[str, Field(description="New location, relative to the project root.")],
    message: Annotated[str, Field(description="Commit message shown in Overleaf's history.")] = "",
) -> dict:
    """Rename or move a file within the attached project. Does not update \\input or \\includegraphics references."""
    ws = _ws()
    src, dst = ws.resolve(path), ws.resolve(new_path)
    if not src.is_file():
        raise ToolError("NOT_FOUND", f"{path} is not a file.")
    if dst.exists():
        raise ToolError("EXISTS", f"{new_path} already exists.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    rel_src = ws.rel(src)
    src.rename(dst)
    res = ws.commit_and_push([rel_src, ws.rel(dst)], _msg(message, f"Move {path} to {new_path}"))
    return {"moved": path, "to": new_path, **res}


# =========================================================================== sync


@tool(annotations=RO)
def sync_project() -> dict:
    """Pull the latest changes made in Overleaf (by the user or collaborators) right now."""
    ws = session.require()
    return ws.sync(force=True)


@tool(annotations=WRITE)
def push_changes() -> dict:
    """Push local commits that have not reached Overleaf yet (only needed when auto-push is off or a push failed)."""
    ws = session.require()
    n = ws.unpushed()
    if n == 0:
        return {"pushed": False, "note": "nothing to push"}
    res = ws.push(drop_on_conflict=False)
    return {"commits": n, **res}


@tool(annotations=DESTRUCTIVE)
def discard_unpushed() -> dict:
    """Throw away local commits that never reached Overleaf and reset to Overleaf's current version."""
    ws = session.require()
    n = ws.unpushed()
    ws.git.run(["fetch", "--quiet", "origin"], cwd=ws.root)
    ws.git.run(["reset", "--hard", "--quiet", "@{u}"], cwd=ws.root)
    ws.git.run(["clean", "-fdq"], cwd=ws.root)
    return {"discarded_commits": n, "head": ws.head()}


# =========================================================================== prompts


@mcp.prompt(name="attach", description="Attach an Overleaf project to this session")
def attach_prompt(project: str) -> str:
    return (
        f"Attach the Overleaf project '{project}' with attach_project, then give me a short "
        "overview of its structure using outline. Do not edit anything yet."
    )


# =========================================================================== entry


def _cleanup() -> None:
    try:
        if session.ws:
            res = session.detach()
            if res.get("warning"):
                log.warning(res["warning"])
    except Exception:  # noqa: BLE001
        log.exception("cleanup failed")


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s overleaf-mcp %(levelname)s %(message)s")
    try:
        removed = prune_stale_work_dirs(session.settings.home)
        if removed:
            log.info("removed stale working copies: %s", ", ".join(removed))
    except Exception:  # noqa: BLE001
        log.exception("pruning stale working copies failed")
    atexit.register(_cleanup)
    mcp.run("stdio")
