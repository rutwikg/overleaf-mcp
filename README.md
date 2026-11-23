# overleaf-mcp

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

An MCP server that lets Claude (Claude Code and Claude Desktop) read and edit
**one Overleaf project at a time**. Edits are pushed straight to Overleaf and
appear in your browser editor within seconds.

It talks to Overleaf through Overleaf's official **Git integration**, which
needs a paid Overleaf plan (or a self-hosted Server Pro). There is no
unofficial scraping of the web editor.

## How access is limited

Three layers, from loosest to tightest:

1. **Registry.** The server can only attach projects you have registered with
   `overleaf-mcp add`. Claude cannot register projects through the MCP tools,
   so it cannot widen its own access.
2. **Attach.** A server process holds at most one attached project. Every file
   path is resolved inside that project's working copy; `..`, absolute paths
   and `.git` are refused. Nothing else is reachable.
3. **Pin.** Set `OVERLEAF_PROJECT` in a server's config and it is locked to that
   one project: it auto-attaches, and `attach_project` / `detach_project` refuse.

Per session: Claude Code starts a separate server process for every session,
so each session has its own attachment and its own working copy. Two sessions
can work on two different projects (or the same one) at once. Claude Desktop
starts one server for all chats, so there an attachment is shared by every
chat until changed.

What this does *not* protect against: the Overleaf Git token is account-wide
(Overleaf has no per-project tokens), and it sits in `~/.overleaf-mcp/token`.
The scoping above is enforced by this server. A Claude Code session that also
has unrestricted shell access could read the token file directly. If that
matters, deny shell reads of `~/.overleaf-mcp` in your Claude Code permissions.

## Setup

### 1. Install

```bash
git clone https://github.com/rutwikg/overleaf-mcp C:\path\to\overleaf-mcp
cd C:\path\to\overleaf-mcp
uv sync
```

The server executable is `C:\path\to\overleaf-mcp\.venv\Scripts\overleaf-mcp.exe`.
Always give clients this full path; they do not inherit your shell's PATH.

### 2. Store your Overleaf Git token

In Overleaf: **Account Settings > Git integration > Generate token**. Then, in a
terminal (the token is read with hidden input and saved to `~/.overleaf-mcp/token`):

```bash
C:\path\to\overleaf-mcp\.venv\Scripts\overleaf-mcp.exe login
```

### 3. Register the projects Claude may use

The alias is what you will say to Claude. The URL is the one in your browser
when the project is open.

```bash
C:\path\to\overleaf-mcp\.venv\Scripts\overleaf-mcp.exe add thesis https://www.overleaf.com/project/0123456789abcdef01234567
```

`add` checks that the token can reach the project. Other commands: `list`, `rename OLD NEW`,
`remove ALIAS`, `check [ALIAS]`.

### 4a. Claude Code: attach per session

Register the server once, for all your sessions:

```bash
claude mcp add --scope user overleaf -- C:\path\to\overleaf-mcp\.venv\Scripts\overleaf-mcp.exe
```

Then, in any session, either say *"attach my thesis project"* or use the slash
command the server provides:

```
/mcp__overleaf__attach thesis
```

That session can now edit `thesis` and nothing else. Another session is
unaffected until you attach something there.

### 4b. Claude Code: pin a folder to one project

To make every session started in a folder work on one project only, put a
`.mcp.json` in that folder (see [`examples/pinned.mcp.json`](examples/pinned.mcp.json)):

```json
{
  "mcpServers": {
    "overleaf": {
      "command": "D:\\Work\\AgenticAI\\OverleafMCP\\.venv\\Scripts\\overleaf-mcp.exe",
      "env": { "OVERLEAF_PROJECT": "thesis" }
    }
  }
}
```

If you also registered the server at user scope, the project-scope entry of
the same name takes precedence in that folder.

### 4c. Claude Desktop

Add to `claude_desktop_config.json` (Settings > Developer > Edit Config) and
restart Claude Desktop:

```json
{
  "mcpServers": {
    "overleaf": {
      "command": "D:\\Work\\AgenticAI\\OverleafMCP\\.venv\\Scripts\\overleaf-mcp.exe"
    }
  }
}
```

Then tell Claude *"attach thesis"* in a chat. Add `"env": {"OVERLEAF_PROJECT": "thesis"}`
to lock Desktop to a single project.

## Tools

| Tool | What it does |
|---|---|
| `list_projects` | Registered aliases and which one is attached |
| `attach_project` / `detach_project` | Bind / unbind this session's project |
| `project_status` | Attached project, sync state, push mode |
| `list_files`, `read_file`, `search_project` | Read the project |
| `outline` | Section structure in reading order, following `\input` / `\include` |
| `edit_file` | Exact find/replace, pushed to Overleaf |
| `apply_edits` | Several find/replace edits to one file as one atomic change |
| `write_file` | Create a file or replace one wholesale |
| `upload_file` | Copy a local file (figure, PDF, .bib) into the project |
| `delete_file`, `move_file` | File management |
| `project_history`, `show_diff` | Overleaf's version history |
| `sync_project`, `push_changes`, `discard_unpushed` | Manual sync control |

## How editing works

On attach the server clones the project into `~/.overleaf-mcp/work/<alias>-<pid>-<id>/`.
Before each operation it pulls if the last pull is more than 15 s old, so
edits you make in the browser are seen. Each write is committed and pushed
immediately. If you edited the same text in the browser at the same moment,
the push is rejected, the server rebases, and if the edits truly collide it
drops its own change and reports `CONFLICT` so Claude re-reads and redoes it.
Your browser edits always win.

The working copy is deleted when the session ends. Copies left behind by a
crashed process are cleaned up on the next start, unless they hold changes
that never reached Overleaf, which are kept for you.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OVERLEAF_PROJECT` | unset | Pin to this alias, URL or id |
| `OVERLEAF_AUTO_PUSH` | `1` | `0` commits locally; `push_changes` sends them |
| `OVERLEAF_SYNC_INTERVAL` | `15` | Seconds before a pull is considered stale |
| `OVERLEAF_ALLOW_UNREGISTERED` | `0` | `1` lets `attach_project` take any id/URL |
| `OVERLEAF_GIT_HOST` | `https://git.overleaf.com` | For self-hosted Server Pro |
| `OVERLEAF_GIT_TOKEN` | unset | Token via env instead of the token file |
| `OVERLEAF_MCP_HOME` | `~/.overleaf-mcp` | Where token, registry and working copies live |
| `OVERLEAF_COMMIT_NAME` / `_EMAIL` | `Claude (overleaf-mcp)` | Local commit identity |

Overleaf's history attributes Git pushes to the token's owner, not to the
commit name.

## Limitations

- Needs Overleaf's Git integration (paid plan or Server Pro).
- Does not compile. Check the PDF in Overleaf.
- Overleaf's main-file setting is not visible over Git; the server guesses the
  file containing `\documentclass`, preferring `main.tex`.
- Overleaf's Git bridge can take a few seconds per push on large projects.

## Development

```bash
uv run pytest
npx @modelcontextprotocol/inspector C:\path\to\overleaf-mcp\.venv\Scripts\overleaf-mcp.exe
```

Tests run against a fake Overleaf made of bare Git repos over `file://`; only
the transport is faked.

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).
