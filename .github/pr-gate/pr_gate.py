"""PR pre-review gate.

Single-file, stdlib-only module so it can be vendored into any repository
(`.github/pr-gate/pr_gate.py`) and also run locally by Claude Code.

The `hook` and `post-push` subcommands run before and after every Bash/PowerShell
call, so their path avoids heavy imports (argparse, subprocess, ...) and returns
right away when the command does not mention a push.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import sys
from collections.abc import Sequence

COMMIT_TYPES = "feat|fix|refactor|test|docs|chore|perf|build|ci|style|revert"
BRANCH_TYPES = "feat|fix|hotfix|refactor|test|docs|chore|perf|ci|spike"
BOT_BRANCH_PREFIXES = ("dependabot/", "renovate/")
MAX_SUMMARY_LEN = 72

WORKFLOW_PATH = ".github/workflows/pr-gate.yml"
INSTRUCTIONS_PATH = ".github/copilot-instructions.md"
APPROVAL_FILE = "pr-gate-approved"
INSTALL_HINT = "python ~/.claude/pr-review-kit/src/install.py repo ."
BASE_BRANCHES = ("HEAD", "main", "master")  # looked up on the pushed remote, then origin
REVIEW_CONTEXT = "pr-reviewer"

_HEADER_RE = re.compile(rf"^(?:{COMMIT_TYPES})(?:\([a-z0-9._/-]+\))?!?: (?P<summary>.+)$")
_BRANCH_RE = re.compile(rf"^(?:{BRANCH_TYPES})/[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*$")
# --- shell command parsing (Bash, PowerShell, cmd) -----------------------------------
# Tokens: redirections, separators and words, which may contain quoted parts.
# Whitespace is skipped. Line continuations are joined first.
# Bash: braces separate only when standalone (keeps ${VAR} and {a,b} inside words), and
# backslash escapes characters, also inside double quotes.
_BASH_TOKEN_RE = re.compile(
    r"""(?P<redir>[0-9*&]?>>?(?:&[0-9])?|<)"""
    r"""|(?P<sep>&&|\|\||[;|&()\n]|[{}](?=\s|$))"""
    r"""|(?P<word>(?:\\.|[^\s;&|()<>"'\\]|"(?:[^"\\]|\\.)*"|'[^']*')+)"""
)
# PowerShell: braces always delimit script blocks (`if ($x) {git push}`); no backslash
# escapes (paths end in `\`).
_POWERSHELL_TOKEN_RE = re.compile(
    r"""(?P<redir>[0-9*&]?>>?(?:&[0-9])?|<)"""
    r"""|(?P<sep>&&|\|\||[;|&(){}\n])"""
    r"""|(?P<word>(?:[^\s;&|(){}<>"']|"[^"]*"|'[^']*')+)"""
)
_BASH_CONTINUATION_RE = re.compile(r"\\\r?\n")
_POWERSHELL_CONTINUATION_RE = re.compile(r"`\r?\n")
# `git -c` settings that make a plain `git push` send every matching branch.
_PUSH_EVERY_BRANCH_CONFIG_RE = re.compile(
    r"push\.default=matching|remote\..+\.push=", re.IGNORECASE
)
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=")
# Words that may precede the actual command: shell keywords and plain wrappers...
_PLAIN_PREFIXES = frozenset(
    {"if", "then", "else", "elif", "while", "until", "do", "!", "command", "exec", "nohup"}
    | {"time", "builtin", "noglob"}
)
# ...and wrappers with their own options (`timeout` also takes a duration).
_OPTION_WRAPPERS = frozenset({"env", "sudo", "doas", "nice", "ionice", "timeout", "stdbuf"})
_WRAPPER_VALUE_OPTS = frozenset({"-u", "-g", "-n", "-s", "-k", "-C", "-h", "-p", "-U", "-c"})
_GIT_PROGRAM_RE = re.compile(r"(?:.*[\\/])?git(?:\.exe)?", re.IGNORECASE)
_GIT_VALUE_OPTS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
# Ways to point git at another repository that the gate does not follow.
_GIT_REDIRECT_OPTS = frozenset({"--git-dir", "--work-tree"})
_GIT_REDIRECT_ENV = frozenset({"GIT_DIR", "GIT_WORK_TREE"})
_NESTED_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "cmd", "powershell", "pwsh", "wsl"})
_NESTED_PUSH_RE = re.compile(r"\bgit(?:\.exe)?\b.*\bpush\b", re.IGNORECASE)
_CD_COMMANDS = frozenset({"cd", "chdir", "sl", "set-location"})
_PUSHD_COMMANDS = frozenset({"pushd", "push-location"})
_POPD_COMMANDS = frozenset({"popd", "pop-location"})
_CD_PATH_PARAMS = frozenset({"-path", "-literalpath", "-lp", "-pspath"})
_CD_VALUE_PARAMS = frozenset(
    {"-erroraction", "-ea", "-warningaction", "-wa", "-informationaction", "-ia"}
    | {"-errorvariable", "-ev", "-warningvariable", "-wv", "-informationvariable", "-iv"}
    | {"-outvariable", "-ov", "-outbuffer", "-ob", "-pipelinevariable", "-pv", "-stackname"}
)
# Git Bash / Cygwin drive paths: /d/x and /cygdrive/d/x mean D:/x on Windows.
_MSYS_DRIVE_RE = re.compile(r"^/(?:cygdrive/)?(?P<drive>[a-zA-Z])(?=/|$)")
# `git push` options that take the next argument as their value.
_PUSH_VALUE_OPTS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
_PUSH_EVERY_BRANCH_OPTS = frozenset({"--all", "--mirror", "--branches"})
_PUSH_DELETE_OPTS = frozenset({"--delete", "-d"})


def check_title(title: str) -> list[str]:
    """Validate a Conventional Commits header (PR title or commit first line)."""
    match = _HEADER_RE.match(title)
    if not match:
        return [f"'{title}' is not '<type>(<scope>): <summary>' with type in {COMMIT_TYPES}"]
    summary = match.group("summary")
    errors = []
    if summary[0].isupper():
        errors.append(f"summary must start lowercase: '{summary}'")
    if summary.endswith("."):
        errors.append(f"summary must not end with a period: '{summary}'")
    if len(title) > MAX_SUMMARY_LEN:
        errors.append(f"header has {len(title)} chars (max {MAX_SUMMARY_LEN})")
    return errors


def check_branch(branch: str) -> list[str]:
    """Validate a Conventional Branch name; bot branches are exempt."""
    if branch.startswith(BOT_BRANCH_PREFIXES) or _BRANCH_RE.match(branch):
        return []
    return [f"branch '{branch}' is not '<type>/<kebab-case>' with type in {BRANCH_TYPES}"]


def check_commit(message: str) -> list[str]:
    """Validate a commit message by its first line; merge/revert commits are exempt."""
    header = message.splitlines()[0] if message else ""
    if header.startswith(("Merge ", 'Revert "')):
        return []
    return check_title(header)


def _run(*command: str, cwd: str | None = None) -> str:
    import subprocess  # noqa: S404 - only runs fixed git/gh commands; imported lazily

    return subprocess.run(  # noqa: S603
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _git(*args: str, cwd: str | None = None) -> str:
    return _run("git", *args, cwd=cwd)


def _gh(*args: str, cwd: str) -> bool:
    import subprocess  # noqa: S404

    try:
        _run("gh", *args, cwd=cwd)
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


def _commit_messages(base: str) -> list[str]:
    raw = _git("log", "--format=%B%x00", f"{base}..HEAD")
    return [m.strip() for m in raw.split("\x00") if m.strip()]


def _try_git(*args: str, cwd: str) -> str | None:
    import subprocess  # noqa: S404

    try:
        return _git(*args, cwd=cwd).strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _resolve(base: str | None, raw: str, windows: bool) -> str | None:
    """`raw` (as typed in a shell) relative to `base`, or None if it cannot be known."""
    if base is None:
        return None
    paths = ntpath if windows else posixpath
    path = paths.expandvars(_unquote(raw))
    if path == "-" or any(char in path for char in "$%`"):
        return None  # previous dir, unset/shell variables or command substitution
    path = paths.expanduser(path)
    if windows:
        path = _MSYS_DRIVE_RE.sub(lambda m: f"{m['drive'].upper()}:/", path)
        if path.startswith("/"):
            return None  # Git Bash path such as /tmp: not a Windows path we can map
    resolved: str = paths.normpath(paths.join(base, path))
    return resolved


def _segments(command: str, powershell: bool) -> list[tuple[list[str], str]]:
    """(words, separator that ends them) for each simple command, redirections dropped."""
    token_re = _POWERSHELL_TOKEN_RE if powershell else _BASH_TOKEN_RE
    continuation_re = _POWERSHELL_CONTINUATION_RE if powershell else _BASH_CONTINUATION_RE
    segments: list[tuple[list[str], str]] = []
    words: list[str] = []
    skip_target = False
    for token in token_re.finditer(continuation_re.sub(" ", command)):
        kind, text = token.lastgroup, token.group()
        if kind == "word":
            if not skip_target:
                words.append(text)
            skip_target = False
        elif kind == "redir":
            skip_target = not text[-1].isdigit()  # `2>&1` carries its own target
        else:
            segments.append((words, text))
            words, skip_target = [], False
    segments.append((words, ""))
    return segments


def _strip_prefixes(words: list[str]) -> tuple[set[str], list[str]]:
    """(env var names assigned in front, the command words) without keywords/wrappers."""
    names: set[str] = set()
    index = 0
    while index < len(words):
        word = _unquote(words[index]).lower()
        if _ASSIGNMENT_RE.match(words[index]):
            names.add(words[index].split("=", 1)[0].upper())
            index += 1
        elif word in _PLAIN_PREFIXES:
            index += 1
        elif word in _OPTION_WRAPPERS:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 2 if words[index] in _WRAPPER_VALUE_OPTS else 1
            if word == "timeout" and index < len(words):
                index += 1  # the duration
        else:
            break
    return names, words[index:]


def _change_dir(directory: str | None, args: list[str], windows: bool) -> str | None:
    """Directory after `cd`/`Set-Location` with `args`, or None if it cannot be known."""
    positional: list[str] = []
    index = 0
    options_done = False
    while index < len(args):
        raw, arg = args[index], _unquote(args[index]).lower()
        if options_done or arg == "-" or not (arg.startswith("-") or arg == "/d"):
            positional.append(raw)
        elif arg == "--":
            options_done = True
        elif arg in _CD_PATH_PARAMS:
            positional.extend(args[index + 1 : index + 2])
            index += 1
        elif arg in _CD_VALUE_PARAMS:
            index += 1
        index += 1  # other options (-P, -L, /d, -PassThru, ...) are just skipped
    if not positional and [_unquote(arg).lower() for arg in args] == ["/d"]:
        positional = args  # alone, `/d` is the Git Bash path of drive D:, not cmd's option
    return _resolve(directory, positional[0], windows) if len(positional) == 1 else None


class Push:
    """A `git push` found in a hook payload (a plain class: dataclasses is slow to import)."""

    __slots__ = ("directory", "every_branch", "remote", "sources")

    def __init__(
        self,
        directory: str | None,
        remote: str | None = None,
        sources: tuple[str, ...] = (),
        every_branch: bool = False,
    ) -> None:
        self.directory = directory  # None: the directory it runs in could not be determined
        self.remote = remote  # first positional argument, if any
        self.sources = sources  # local refs/commits it sends (deletions excluded)
        self.every_branch = every_branch  # --all / --mirror / --branches


def parse_push_args(args: Sequence[str]) -> tuple[str | None, tuple[str, ...], bool]:
    """(remote, sources, every_branch) for the arguments that follow `git push`."""
    positional: list[str] = []
    flags: set[str] = set()
    takes_value = False
    for arg in args:
        if takes_value:
            takes_value = False
        elif arg in _PUSH_VALUE_OPTS:
            takes_value = True
        elif arg.startswith("-"):
            flags.add(arg.split("=", 1)[0])
        else:
            positional.append(arg)
    remote = positional[0] if positional else None
    if flags & _PUSH_EVERY_BRANCH_OPTS:
        return remote, (), True
    if flags & _PUSH_DELETE_OPTS:
        return remote, (), False
    refspecs = positional[1:]
    if any(spec in (":", "+:") for spec in refspecs):
        return remote, (), True  # ":" pushes every branch that matches on the remote
    if not refspecs:  # `git push [remote]` sends the current branch; `--tags` only tags
        return remote, (() if "--tags" in flags else ("HEAD",)), False
    sources = (spec.lstrip("+").split(":", 1)[0] for spec in refspecs)
    return remote, tuple(source for source in sources if source), False  # ":x" deletes x


def _git_push(args: list[str], directory: str | None, windows: bool) -> Push | None:
    """The Push for `git <args>` run in `directory`, or None if it is not a push."""
    index = 0
    matching_config = False
    while index < len(args):
        option = _unquote(args[index])
        name = option.split("=", 1)[0]
        if name in _GIT_REDIRECT_OPTS:
            directory = None
        if not option.startswith("-"):
            break
        if name in _GIT_VALUE_OPTS and "=" not in option:
            value = _unquote(args[index + 1]) if index + 1 < len(args) else ""
            if option == "-C":
                directory = _resolve(directory, value, windows)
            elif option == "-c" and _PUSH_EVERY_BRANCH_CONFIG_RE.match(value):
                matching_config = True
            index += 1
        index += 1
    if index >= len(args) or _unquote(args[index]).lower() != "push":
        return None
    remote, sources, every_branch = parse_push_args([_unquote(a) for a in args[index + 1 :]])
    if matching_config and sources == ("HEAD",):  # no refspec: the config decides what goes
        every_branch, sources = True, ()
    return Push(directory, remote, sources, every_branch)


def find_pushes(
    command: str, cwd: str, windows: bool = os.name == "nt", powershell: bool = False
) -> tuple[list[Push], str | None]:
    """Every `git push` in a shell command, each with the directory it runs in.

    Follows directory changes in order (`cd`, `chdir`, `cd /d`, `Set-Location`/`sl`,
    `pushd`/`popd` and `Push-/Pop-Location` with a stack, bash subshells) and each
    push's own `git -C`. A directory that cannot be known becomes None, and so does
    one chosen by --git-dir/--work-tree/GIT_DIR/GIT_WORK_TREE. A push inside a nested
    shell (`bash -c "git push"`) is reported with directory None. Also returns the
    directory the command ends in.
    """
    directory: str | None = cwd
    pushd_stack: list[str | None] = []
    subshells: list[str | None] = []
    pushes: list[Push] = []
    for words, separator in _segments(command, powershell):
        names, words = _strip_prefixes(words)
        program = _unquote(words[0]) if words else ""
        name = program.lower().removesuffix(".exe")
        if name in _CD_COMMANDS or name in _PUSHD_COMMANDS:
            if name in _PUSHD_COMMANDS:
                pushd_stack.append(directory)
            directory = _change_dir(directory, words[1:], windows)
        elif name in _POPD_COMMANDS:
            directory = pushd_stack.pop() if pushd_stack else None
        elif _GIT_PROGRAM_RE.fullmatch(program):
            run_in = None if names & _GIT_REDIRECT_ENV else directory
            if push := _git_push(words[1:], run_in, windows):
                pushes.append(push)
        elif name in _NESTED_SHELLS and _NESTED_PUSH_RE.search(" ".join(words[1:])):
            pushes.append(Push(None))
        if separator == "(" and not powershell:  # in PowerShell ( ) is just grouping
            subshells.append(directory)
        elif separator == ")" and subshells and not powershell:
            directory = subshells.pop()
    return pushes, directory


def target_dir(
    command: str, cwd: str, windows: bool = os.name == "nt", powershell: bool = False
) -> str | None:
    """Directory the first `git push` of a command runs in (None if unknown)."""
    pushes, final = find_pushes(command, cwd, windows, powershell)
    return pushes[0].directory if pushes else final


def _pushes_from_hook_input(stdin_text: str) -> list[Push]:
    """The `git push` commands in a hook payload; unknown/missing directories are None."""
    if "push" not in stdin_text.lower():  # fast path for the vast majority of commands
        return []
    try:
        payload = json.loads(stdin_text)
        command = str(payload["tool_input"]["command"])
        cwd = str(payload.get("cwd") or os.getcwd())
        powershell = payload.get("tool_name") == "PowerShell"
    except (ValueError, KeyError, TypeError, AttributeError):
        return []
    pushes, _ = find_pushes(command, cwd, powershell=powershell)
    for push in pushes:
        if push.directory is not None and not os.path.isdir(push.directory):
            push.directory = None
    return pushes


def _repo_dir(push: Push) -> str | None:
    """The push's directory if it is inside a git repository."""
    if push.directory is None:
        return None
    if _try_git("rev-parse", "--git-dir", cwd=push.directory) is None:
        return None
    return push.directory


def _approval_path(directory: str) -> str:
    git_path = _git("rev-parse", "--git-path", APPROVAL_FILE, cwd=directory).strip()
    return os.path.join(directory, git_path)


def _approved_sha(directory: str) -> str:
    """The commit the pr-reviewer agent approved in this repository ('' if none)."""
    approval = _approval_path(directory)
    if not os.path.exists(approval):
        return ""
    with open(approval, encoding="utf-8") as file:
        return file.read().strip()


def _commit_of(directory: str, ref: str) -> str | None:
    return _try_git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=directory)


def _base_ref(directory: str, remote: str | None) -> str | None:
    """The default branch of the pushed remote (falling back to origin), if known."""
    remotes = dict.fromkeys(name for name in (remote, "origin") if name)
    for name in remotes:
        for branch in BASE_BRANCHES:
            if _commit_of(directory, f"{name}/{branch}"):
                return f"{name}/{branch}"
    return None


def push_block_reason(directory: str, push: Push) -> str | None:
    """Why `push` from `directory` must be blocked, or None to allow it."""
    if push.every_branch:
        return (
            "pushing every branch at once (--all/--mirror/--branches) cannot be checked. "
            "Push one branch at a time, once it has been reviewed."
        )
    base = _base_ref(directory, push.remote)
    if base is None:  # bootstrap: nothing on the remote to compare against yet
        return None
    approved = _approved_sha(directory)
    for source in push.sources:
        sha = _commit_of(directory, source)
        if sha is None:
            return f"could not resolve '{source}' to a commit, so it cannot be checked."
        if _try_git("rev-list", "--count", f"{base}..{sha}", cwd=directory) == "0":
            continue  # nothing new compared to the base branch
        if _try_git("cat-file", "-e", f"{sha}:{WORKFLOW_PATH}", cwd=directory) is None:
            return (
                f"This repository has no PR gate ({WORKFLOW_PATH}). Install it on this branch "
                f"with `{INSTALL_HINT}`, commit it as 'ci(pr-gate): install pr pre-review "
                "gate', then run the pr-reviewer agent."
            )
        if sha != approved:
            return (
                f"'{source}' ({sha[:12]}) has not been approved by the pr-reviewer agent. "
                "Run the pr-reviewer agent on this branch, fix every blocking finding (one "
                "commit per fix), and repeat until it approves; it records the approval itself."
            )
    return None


def _hook_block_reason(push: Push) -> str | None:
    if push.directory is None:  # fail closed: never wave through a push we cannot check
        return (
            "could not determine which repository this `git push` runs in (or it runs in a "
            "nested shell). Run it directly as `git -C <absolute path of the repository> "
            "push ...` so the gate can check it."
        )
    directory = _repo_dir(push)
    return push_block_reason(directory, push) if directory else None


def cmd_hook() -> int:
    """Claude Code PreToolUse hook: exit 2 blocks an unreviewed `git push`."""
    for push in _pushes_from_hook_input(sys.stdin.read()):
        if reason := _hook_block_reason(push):
            print(f"pr-gate: push blocked. {reason}", file=sys.stderr)
            return 2
    return 0


def cmd_post_push() -> int:
    """Claude Code PostToolUse hook: publish the approval as a commit status on GitHub."""
    published: set[tuple[str, str]] = set()
    for push in _pushes_from_hook_input(sys.stdin.read()):
        directory = _repo_dir(push)
        approved = _approved_sha(directory) if directory else ""
        if not directory or not approved or (directory, approved) in published:
            continue
        if approved not in {_commit_of(directory, source) for source in push.sources}:
            continue
        published.add((directory, approved))
        ok = _gh(
            "api",
            "-X",
            "POST",
            f"repos/{{owner}}/{{repo}}/statuses/{approved}",
            "-f",
            "state=success",
            "-f",
            f"context={REVIEW_CONTEXT}",
            "-f",
            "description=Approved by the pr-reviewer agent before push",
            cwd=directory,
        )
        if not ok:
            print(f"pr-gate: could not publish the {REVIEW_CONTEXT} status", file=sys.stderr)
    return 0


def cmd_approve() -> int:
    head = _git("rev-parse", "HEAD").strip()
    with open(_approval_path("."), "w", encoding="utf-8") as file:
        file.write(head + "\n")
    print(f"pr-gate: approved {head}")
    return 0


def cmd_conventions(base: str, title: str | None, branch: str | None) -> int:
    branch = branch or _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    errors = [f"branch: {e}" for e in check_branch(branch)]
    if title is not None:
        errors += [f"title: {e}" for e in check_title(title)]
    for message in _commit_messages(base):
        header = message.splitlines()[0]
        errors += [f"commit '{header}': {e}" for e in check_commit(message)]
    if errors:
        print("conventions: FAILED")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("conventions: OK")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Hot path: the hooks run on every Bash/PowerShell call, so they skip argparse.
    if args == ["hook"]:
        return cmd_hook()
    if args == ["post-push"]:
        return cmd_post_push()

    import argparse

    parser = argparse.ArgumentParser(prog="pr_gate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    conv = sub.add_parser("conventions", help="check PR title, branch name and commits")
    conv.add_argument("--base", required=True, help="base ref, e.g. origin/main")
    conv.add_argument("--title", help="PR title (optional when running locally)")
    conv.add_argument("--branch", help="branch name (default: current branch)")
    sub.add_parser("approve", help="record pr-reviewer approval of HEAD")
    sub.add_parser("hook", help="PreToolUse hook: block unreviewed pushes")
    sub.add_parser("post-push", help="PostToolUse hook: publish approval status")

    parsed = parser.parse_args(args)
    if parsed.command == "conventions":
        return cmd_conventions(parsed.base, parsed.title, parsed.branch)
    if parsed.command == "approve":
        return cmd_approve()
    parser.error(f"'{parsed.command}' takes no arguments")


if __name__ == "__main__":
    sys.exit(main())
