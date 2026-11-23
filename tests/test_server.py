"""End-to-end tests against a fake Overleaf: bare git repos reached over file://.

Only the transport is faked. The tools, the git plumbing, the push/rebase and
conflict handling all run for real.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from overleaf_mcp import server
from overleaf_mcp.config import Registry, Settings
from overleaf_mcp.project import Session

PID_A = "a" * 24
PID_B = "b" * 24
MAIN = r"""\documentclass{article}
\begin{document}
\section{Introduction}
Hello world.
\input{chapters/method}
\end{document}
"""
METHOD = "\\section{Method}\nWe did things.\n\\subsection{Details}\nMore.\n"


def git(*args, cwd=None):
    # bytes, not text=True: text mode would translate \r\n and hide line-ending bugs
    out = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True, capture_output=True).stdout
    return out.decode("utf-8")


def make_remote(remotes: Path, pid: str, files: dict[str, str]) -> Path:
    bare = remotes / pid
    git("init", "--bare", "-q", "-b", "master", str(bare))
    seed = remotes / f"seed-{pid}"
    git("clone", "-q", str(bare), str(seed))
    for rel, text in files.items():
        p = seed / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
    git("-c", "user.name=Overleaf", "-c", "user.email=o@x", "add", "-A", cwd=seed)
    git("-c", "user.name=Overleaf", "-c", "user.email=o@x", "commit", "-qm", "init", cwd=seed)
    git("push", "-q", "origin", "HEAD:master", cwd=seed)
    return bare


def browser_edit(tmp: Path, bare: Path, rel: str, text: str, name="browser") -> None:
    """Simulate the user editing in Overleaf: a separate clone pushes a change."""
    d = tmp / name
    if not d.exists():
        git("clone", "-q", str(bare), str(d))
    else:
        git("pull", "-q", cwd=d)
    (d / rel).write_text(text, encoding="utf-8", newline="")
    git("-c", "user.name=U", "-c", "user.email=u@x", "commit", "-qam", "browser edit", cwd=d)
    git("push", "-q", "origin", "HEAD:master", cwd=d)


def remote_file(bare: Path, rel: str) -> str:
    return git("show", f"master:{rel}", cwd=bare)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    bare_a = make_remote(remotes, PID_A, {"main.tex": MAIN, "chapters/method.tex": METHOD})
    bare_b = make_remote(remotes, PID_B, {"main.tex": "secret project B\n"})
    reg = Registry(home / "projects.json")
    reg.add("thesis", f"https://www.overleaf.com/project/{PID_A}")
    reg.add("other", PID_B)

    def install(**overrides):
        s = Settings(git_host="file:///" + remotes.as_posix(), home=home, **{"sync_interval": 0, **overrides})
        monkeypatch.setattr(server, "session", Session(settings=s, registry=reg))
        return server.session

    install()
    return {"tmp": tmp_path, "home": home, "a": bare_a, "b": bare_b, "install": install}


def call(name: str, **args) -> dict:
    res = asyncio.run(server.mcp.call_tool(name, args))
    return json.loads(res.content[0].text)


# ---------------------------------------------------------------- scoping


def test_tools_refuse_before_attach(env):
    r = call("read_file", path="main.tex")
    assert r["ok"] is False and r["error"] == "NOT_ATTACHED"


def test_unregistered_project_is_refused(env):
    r = call("attach_project", project="c" * 24)
    assert r["error"] == "NOT_REGISTERED"


def test_attach_by_alias_and_read(env):
    r = call("attach_project", project="thesis")
    assert r["ok"] and r["attached"] == "thesis" and r["main_file"] == "main.tex"
    assert "chapters/method.tex" in r["files"]
    r = call("read_file", path="chapters/method.tex")
    assert r["content"] == METHOD and r["total_lines"] == 4


@pytest.mark.parametrize("bad", ["../other/main.tex", "/etc/passwd", "C:/Windows/win.ini", ".git/config", "a/../../x"])
def test_path_escape_is_refused(env, bad):
    call("attach_project", project="thesis")
    assert call("read_file", path=bad)["error"] == "BAD_PATH"
    assert call("write_file", path=bad, content="x")["error"] == "BAD_PATH"


def test_switching_projects_drops_the_old_working_copy(env):
    first = Path(call("attach_project", project="thesis") and server.session.ws.root)
    call("attach_project", project="other")
    assert not first.exists()
    assert call("read_file", path="main.tex")["content"] == "secret project B\n"


def test_pinned_server_refuses_other_projects(env):
    env["install"](pinned="thesis")
    r = call("list_files")  # auto-attaches the pinned project
    assert r["ok"] and r["project"] == "thesis"
    r = call("attach_project", project="other")
    assert r["error"] == "PINNED"
    assert call("detach_project")["error"] == "PINNED"


def test_pinned_by_raw_id_needs_no_registry(env):
    env["install"](pinned=PID_B)
    assert call("read_file", path="main.tex")["content"] == "secret project B\n"


# ---------------------------------------------------------------- editing


def test_edit_is_pushed_to_overleaf(env):
    call("attach_project", project="thesis")
    r = call("edit_file", path="main.tex", old_text="Hello world.", new_text="Hello Overleaf.", message="Greet")
    assert r["ok"] and r["pushed"] is True, r
    assert "Hello Overleaf." in remote_file(env["a"], "main.tex")
    assert git("log", "-1", "--format=%s", "master", cwd=env["a"]).strip() == "Greet"


def test_edit_no_match_and_ambiguous(env):
    call("attach_project", project="thesis")
    assert call("edit_file", path="main.tex", old_text="nope", new_text="x")["error"] == "NO_MATCH"
    assert call("edit_file", path="main.tex", old_text="document", new_text="x")["error"] == "AMBIGUOUS"


def test_apply_edits_is_atomic(env):
    call("attach_project", project="thesis")
    r = call("apply_edits", path="main.tex", edits=[
        {"old_text": "Hello world.", "new_text": "A."},
        {"old_text": "does not exist", "new_text": "B."},
    ])
    assert r["error"] == "NO_MATCH"
    assert "Hello world." in remote_file(env["a"], "main.tex")
    r = call("apply_edits", path="main.tex", edits=[
        {"old_text": "Hello world.", "new_text": "A."},
        {"old_text": "Introduction", "new_text": "Intro"},
    ])
    assert r["ok"] and r["replacements"] == 2
    assert "\\section{Intro}\nA." in remote_file(env["a"], "main.tex")


def test_write_new_file_and_delete(env):
    call("attach_project", project="thesis")
    assert call("write_file", path="chapters/results.tex", content="\\section{Results}\n")["created"] is True
    assert remote_file(env["a"], "chapters/results.tex") == "\\section{Results}\n"
    assert call("delete_file", path="chapters/results.tex")["ok"]
    with pytest.raises(subprocess.CalledProcessError):
        remote_file(env["a"], "chapters/results.tex")


def test_crlf_files_keep_their_line_endings(env):
    browser_edit(env["tmp"], env["a"], "main.tex", "line one\r\nline two\r\n")
    call("attach_project", project="thesis")
    r = call("edit_file", path="main.tex", old_text="line one\nline two", new_text="line 1\nline 2")
    assert r["ok"], r
    assert remote_file(env["a"], "main.tex") == "line 1\r\nline 2\r\n"


def test_browser_edits_are_seen(env):
    call("attach_project", project="thesis")
    browser_edit(env["tmp"], env["a"], "main.tex", MAIN.replace("Hello world.", "Edited in browser."))
    assert "Edited in browser." in call("read_file", path="main.tex")["content"]


def test_concurrent_edit_to_another_file_is_rebased(env):
    env["install"](sync_interval=10_000)  # do not pull before the write: force the race
    call("attach_project", project="thesis")
    browser_edit(env["tmp"], env["a"], "chapters/method.tex", "browser changed method\n")
    r = call("edit_file", path="main.tex", old_text="Hello world.", new_text="Claude was here.")
    assert r["ok"] and r["pushed"], r
    assert remote_file(env["a"], "chapters/method.tex") == "browser changed method\n"
    assert "Claude was here." in remote_file(env["a"], "main.tex")


def test_concurrent_edit_to_same_line_is_a_clean_conflict(env):
    env["install"](sync_interval=10_000)
    call("attach_project", project="thesis")
    browser_edit(env["tmp"], env["a"], "main.tex", MAIN.replace("Hello world.", "Browser wins."))
    r = call("edit_file", path="main.tex", old_text="Hello world.", new_text="Claude wins.")
    assert r["error"] == "CONFLICT", r
    assert "Browser wins." in remote_file(env["a"], "main.tex")
    # The working copy was reset to Overleaf's version, so a re-read shows the truth.
    assert call("project_status")["unpushed_commits"] == 0
    assert "Browser wins." in call("read_file", path="main.tex")["content"]


def test_auto_push_off_batches_until_push(env):
    env["install"](auto_push=False)
    call("attach_project", project="thesis")
    r = call("edit_file", path="main.tex", old_text="Hello world.", new_text="Batched.")
    assert r["pushed"] is False and r["unpushed_commits"] == 1
    assert "Hello world." in remote_file(env["a"], "main.tex")
    assert call("push_changes")["pushed"] is True
    assert "Batched." in remote_file(env["a"], "main.tex")


# ---------------------------------------------------------------- navigation


def test_outline_follows_input(env):
    call("attach_project", project="thesis")
    r = call("outline")
    titles = [(i["kind"], i["title"], i["file"]) for i in r["outline"]]
    assert titles == [
        ("section", "Introduction", "main.tex"),
        ("section", "Method", "chapters/method.tex"),
        ("subsection", "Details", "chapters/method.tex"),
    ]


def test_search(env):
    call("attach_project", project="thesis")
    r = call("search_project", pattern="section{M", file_glob="*.tex")
    assert r["matches"] == [{"path": "chapters/method.tex", "line": 1, "text": "\\section{Method}"}]


def test_detach_removes_working_copy(env):
    call("attach_project", project="thesis")
    root = server.session.ws.root
    assert call("detach_project")["detached"] is True
    assert not root.exists()
    assert call("list_files")["error"] == "NOT_ATTACHED"


def test_rename_alias(env):
    from overleaf_mcp.config import ConfigError

    reg = server.session.registry
    reg.rename("thesis", "phd")
    assert call("attach_project", project="phd")["attached"] == "phd"
    assert call("attach_project", project="thesis")["error"] == "NOT_REGISTERED"
    with pytest.raises(ConfigError):
        reg.rename("phd", "other")  # taken
