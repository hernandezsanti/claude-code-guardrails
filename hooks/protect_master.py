#!/usr/bin/env python3
"""Block git operations that change master/main without explicit authorization.

Claude Code PreToolUse hook for the Bash tool. Exit code 2 + stderr = block.

A naive version would look at the whole command as plain text: a push of
`fix/ABC-1-x` would be blocked if the same command also had a `git fetch origin
master`, a PR body with the word "main", or a path like `ABC-1-prod-master`.
This one splits the command into segments (outside quotes and heredocs),
follows `cd` and `git checkout` in order, and decides per segment:

1. merge/reset/rebase while on master/main -> block.
2. push whose destination is master/main (explicit refspec, or the current
   branch if there is no refspec) -> block.
3. --all / --mirror -> block.

For tests: PROTECT_MASTER_BRANCHES='{"/path": "branch"}' sets the branch of
each directory without asking git.
"""
import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = {"master", "main"}
REWRITE = {"merge", "reset", "rebase"}


def strip_heredocs(command):
    """Remove heredoc bodies; keep the line that opens them."""
    lines = command.split("\n")
    out = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        m = re.search(r"<<-?\s*\\?['\"]?(\w+)['\"]?", lines[i])
        i += 1
        if m:
            while i < len(lines) and lines[i].strip() != m.group(1):
                i += 1
            i += 1
    return "\n".join(out)


def segments(command):
    """Split on &&, ||, ;, |, & and newlines outside quotes."""
    segs, cur, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                cur.append(command[i + 1])
                i += 1
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in ";&|\n":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    out = []
    for s in segs:
        try:
            toks = shlex.split(s, comments=True)
        except ValueError:
            toks = s.split()
        if toks:
            out.append(toks)
    return out


def resolve(path, cwd):
    path = os.path.expanduser(path)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


class Branches:
    def __init__(self):
        self.override = json.loads(os.environ.get("PROTECT_MASTER_BRANCHES") or "null")
        self.cache = {}

    def get(self, d):
        if d not in self.cache:
            if self.override is not None:
                self.cache[d] = self.override.get(d, "")
            else:
                r = subprocess.run(["git", "-C", d, "rev-parse", "--abbrev-ref", "HEAD"],
                                   capture_output=True, text=True)
                self.cache[d] = r.stdout.strip() if r.returncode == 0 else ""
        return self.cache[d]

    def set(self, d, branch):
        self.cache[d] = branch


def parse_git(toks, cwd):
    """('verb', args, dir) for `git [-C d] [-c k=v] verb ...`, or None."""
    # Skip environment assignments and common wrappers (`rtk`, `env`).
    while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in ("rtk", "env", "command", "proxy")):
        toks = toks[1:]
    if not toks or os.path.basename(toks[0]) != "git":
        return None
    i, d = 1, cwd
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-C" and i + 1 < len(toks):
            d = resolve(toks[i + 1], d)
            i += 2
        elif toks[i] == "-c" and i + 1 < len(toks):
            i += 2
        else:
            i += 1
    if i >= len(toks):
        return None
    return toks[i], toks[i + 1:], d


PUSH_FLAGS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def push_destinations(args, branch):
    """Destination branches of a `git push`, or {'*'} for --all/--mirror."""
    pos, delete, i = [], False, 0
    while i < len(args):
        a = args[i]
        if a in ("--all", "--mirror", "--branches"):
            return {"*"}
        if a in ("-d", "--delete"):
            delete = True
        elif a in PUSH_FLAGS_WITH_VALUE:
            i += 1
        elif not a.startswith("-"):
            pos.append(a)
        i += 1
    refspecs = pos[1:]
    if not refspecs:
        return {branch} if not delete else set()
    dst = set()
    for r in refspecs:
        r = r.lstrip("+")
        dest = r.split(":", 1)[1] if ":" in r else r
        if dest in ("HEAD", "@"):
            dest = branch
        dst.add(re.sub(r"^refs/heads/", "", dest))
    return dst


def reset_rewrites(args):
    if any(a in ("--hard", "--soft", "--mixed", "--merge", "--keep") for a in args):
        return True
    return any(re.match(r"^(HEAD[~^@]|origin/|upstream/|[0-9a-f]{7,40}$)", a) for a in args)


def evaluate(command, cwd):
    """Reason for the block, or None."""
    branches = Branches()
    variables = {}
    for toks in segments(strip_heredocs(command)):
        # `W=/path; git -C $W push ...`: expand variables set in the same command.
        if all(re.match(r"^[A-Za-z_]\w*=", t) for t in toks):
            for t in toks:
                k, v = t.split("=", 1)
                variables[k] = v
            continue
        toks = [re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: variables.get(m.group(1), os.environ.get(m.group(1), m.group(0))), t)
                for t in toks]
        # `for ...; do git merge ...`, `if ...; then git push ...`, `( cd x && ... )`
        while toks and toks[0] in ("do", "then", "else", "elif", "if", "while", "until", "{", "(", "!", "time"):
            toks = toks[1:]
        if toks and toks[0].startswith("(") and len(toks[0]) > 1:
            toks = [toks[0][1:]] + toks[1:]
        if not toks:
            continue
        if toks[0] in ("cd", "pushd") and len(toks) > 1:
            cwd = resolve(toks[1], cwd)
            continue
        g = parse_git(toks, cwd)
        if not g:
            continue
        verb, args, d = g
        branch = branches.get(d)
        if verb in ("checkout", "switch"):
            flags = [a for a in args if a.startswith("-")]
            names = [a for a in args if not a.startswith("-")]
            if "--" in args or not names:
                continue
            if any(f in ("-b", "-B", "-c", "-C", "--create", "--orphan") for f in flags):
                branches.set(d, names[0])
            elif "--detach" in flags:
                branches.set(d, "HEAD")
            elif len(names) == 1:
                branches.set(d, names[0])
            continue
        if verb in REWRITE:
            if any(a in ("--abort", "--quit", "--continue", "--skip") for a in args):
                continue
            if verb == "reset" and not reset_rewrites(args):
                continue
            if branch in PROTECTED:
                return f"git {verb} with current branch '{branch}' (in {d})."
        if verb == "push":
            dst = push_destinations(args, branch)
            if "*" in dst:
                return "git push --all/--mirror includes master/main."
            touched = dst & PROTECTED
            if touched:
                return f"git push to '{sorted(touched)[0]}' (current branch '{branch}', in {d})."
    return None


def main():
    data = json.load(sys.stdin)
    command = (data.get("tool_input") or {}).get("command", "")
    cwd = data.get("cwd") or os.getcwd()
    if not re.search(r"\b(merge|push|reset|rebase)\b", command):
        return 0
    try:
        reason = evaluate(command, cwd)
    except Exception as e:  # on a parser bug, fall back to a coarse rule: fail closed
        if not re.search(r"\b(push|merge|reset|rebase)\b.*\b(master|main)\b", command):
            return 0
        reason = f"could not parse the command ({e}) and it names master/main."
    if not reason:
        return 0
    print(
        f"BLOCKED: {reason}\n"
        "Changes to master/main need explicit authorization from the human. "
        "Ask the user to run this command themselves with `!`.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
