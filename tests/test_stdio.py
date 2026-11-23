"""Launch the real server over stdio, the way Claude Desktop and Claude Code do."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from test_server import MAIN, PID_A, make_remote, remote_file


def test_pinned_server_over_stdio(tmp_path):
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    bare = make_remote(remotes, PID_A, {"main.tex": MAIN})
    env = {
        **os.environ,
        "OVERLEAF_MCP_HOME": str(tmp_path / "home"),
        "OVERLEAF_GIT_HOST": "file:///" + remotes.as_posix(),
        "OVERLEAF_PROJECT": PID_A,
        "OVERLEAF_GIT_TOKEN": "",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "overleaf_mcp"], env=env)

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                assert {"attach_project", "edit_file", "read_file"} <= tools
                res = await s.call_tool("edit_file", {"path": "main.tex", "old_text": "Hello world.",
                                                      "new_text": "Over stdio.", "message": "stdio test"})
                out = json.loads(res.content[0].text)
                assert out["ok"] and out["pushed"], out
                res = await s.call_tool("attach_project", {"project": "b" * 24})
                assert json.loads(res.content[0].text)["error"] == "PINNED"
                prompts = {p.name for p in (await s.list_prompts()).prompts}
                assert "attach" in prompts

    asyncio.run(run())
    assert "Over stdio." in remote_file(bare, "main.tex")
    # working copy cleaned up when the server exited
    work = tmp_path / "home" / "work"
    assert not work.exists() or not any(work.iterdir())
