"""Thin wrapper around the git CLI with Overleaf authentication.

The token never touches disk inside the working copy and never appears on a
command line (where any process can read it). It is handed to git as an HTTP
``Authorization`` header through ``GIT_CONFIG_*`` environment variables, which
git treats as command-scoped config. Credential helpers are disabled for the
same command so Git Credential Manager neither pops up a dialog nor stores the
token.
"""

from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    def __init__(self, code: str, message: str, remediation: str = "", stderr: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.stderr = stderr


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def _classify(stderr: str) -> tuple[str, str]:
    s = stderr.lower()
    if (
        "authentication failed" in s
        or "could not read username" in s
        or "could not read password" in s
        or " 401" in s
        or " 403" in s
    ):
        return (
            "AUTH_FAILED",
            "The Overleaf Git token was rejected. Create a new token under Overleaf "
            "Account Settings > Git integration and run `overleaf-mcp login` in a terminal.",
        )
    if "repository not found" in s or " 404" in s or "does not appear to be a git repository" in s:
        return (
            "PROJECT_NOT_FOUND",
            "Overleaf did not find this project for this token. Check the project id, and "
            "that the token's account owns or can edit the project.",
        )
    if "could not resolve host" in s or "failed to connect" in s or "timed out" in s:
        return ("NETWORK", "Could not reach Overleaf. Check the network connection and retry.")
    if "non-fast-forward" in s or "fetch first" in s or "[rejected]" in s:
        return ("PUSH_REJECTED", "Overleaf has newer changes. Sync and retry.")
    return ("GIT_ERROR", "")


def summarise_error(text: str) -> str:
    """The most informative line of git's stderr: the first 'fatal:'/'error:' line.

    The last line is often a trailing hint ('...and the repository exists.')
    that says nothing on its own.
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    for prefix in ("fatal:", "error:", "remote:"):
        for l in lines:
            if l.lower().startswith(prefix):
                return l
    return lines[0] if lines else ""


class Git:
    def __init__(self, token: str | None, timeout: float = 120.0):
        self.token = token
        self.timeout = timeout

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_ASKPASS"] = ""
        env["SSH_ASKPASS"] = ""
        keys: list[tuple[str, str]] = [("credential.helper", "")]
        if self.token:
            basic = base64.b64encode(f"git:{self.token}".encode()).decode()
            keys.append(("http.extraHeader", f"Authorization: Basic {basic}"))
        env["GIT_CONFIG_COUNT"] = str(len(keys))
        for i, (k, v) in enumerate(keys):
            env[f"GIT_CONFIG_KEY_{i}"] = k
            env[f"GIT_CONFIG_VALUE_{i}"] = v
        return env

    def run(self, args: list[str], cwd: Path | None = None, check: bool = True) -> GitResult:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd else None,
                env=self._env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise GitError("GIT_MISSING", "git is not installed or not on PATH.",
                           "Install Git for Windows and restart the client.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError("TIMEOUT", f"git {args[0]} timed out after {self.timeout:.0f}s.",
                           "Overleaf's Git bridge can be slow for large projects; retry.") from exc
        res = GitResult(proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
            code, remedy = _classify(proc.stderr)
            msg = self._scrub(summarise_error(proc.stderr or proc.stdout) or f"git {args[0]} failed")
            raise GitError(code, f"git {args[0]}: {msg}", remedy, stderr=self._scrub(proc.stderr))
        return res

    def _scrub(self, text: str) -> str:
        if self.token:
            text = text.replace(self.token, "***")
        return text
