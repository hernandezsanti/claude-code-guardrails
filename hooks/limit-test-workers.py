#!/usr/bin/python3
# Claude Code PreToolUse hook for the Bash tool.
#
# Blocks test runners that run in parallel by default when the command does
# not limit concurrency, or when it asks for more workers than the machine can
# handle right now. Each test worker can use around 1.5 GB of RAM, so a single
# `npx jest` without --maxWorkers on a 10-core machine can open 9 workers and
# freeze a 16 GB machine, especially when several agents run tests at once.
#
# How it decides:
# - Splits the command into segments (; && || | & newlines, subshells,
#   braces and $(...), always outside quotes), drops heredoc bodies and
#   comments, and analyzes the content of `bash -c "..."` again.
# - In each segment it skips VAR=x assignments, shell keywords and wrappers
#   (timeout N, nohup, nice, xargs, rtk...), and only recognizes the runner in
#   command position: `npm i -D jest` or `grep jest` do not count.
# - `npm/pnpm/yarn/bun test` and `run <script>` are resolved by reading
#   package.json, following scripts that call other scripts.
# - The limit is computed from the real free memory (kern.memorystatus_level)
#   and the test workers that are already running in other sessions.
# - Used swap also lowers the limit: the memory level alone is not enough,
#   because the system can freeze with several GB of swap while the level
#   still looks fine. macOS does not release swap right away, so the lower
#   limit can last for a while after the peak. This is on purpose.
#
# Uses /usr/bin/python3 on purpose: a pyenv shim adds about 0.45 s to every
# Bash command. On an unexpected error it lets the command through and writes
# the error to limit-test-workers.log, so a bug never blocks the whole shell.

import json
import os
import re
import shlex
import subprocess
import sys

LOG = os.path.expanduser("~/.claude/hooks/limit-test-workers.log")

# Workers of ~1.5 GB on the whole machine, counting what is already running.
TOTAL_BUDGET = int(os.environ.get("CLAUDE_TEST_WORKER_BUDGET", "6"))
MAX_PER_COMMAND = 4
# Used swap (MB) from which the maximum drops to 2 and to 1 worker.
SWAP_CAP_2_MB = 2048
SWAP_CAP_1_MB = 4096

RUNNERS = {"jest", "vitest", "playwright", "tsx", "node", "parallel_rspec",
           "parallel_test", "turbo", "nx", "lerna", "rails"}
KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "time"}
SIMPLE_WRAPPERS = {"nohup", "command", "exec", "rtk", "proxy", "sudo", "caffeinate",
                   "stdbuf", "arch", "cross-env", "dotenv"}
INFO_FLAGS = {"--version", "-v", "--help", "-h", "--listTests", "--clearCache",
              "--showConfig", "--list"}
PKG_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
SHELLS = {"bash", "sh", "zsh"}


# ---------------------------------------------------------------- limit

_limit_cache = {}


def swap_used_mb():
    """Used swap in MB, or None if it cannot be read."""
    if "swap" not in _limit_cache:
        try:
            out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True,
                                 text=True, timeout=2).stdout
            m = re.search(r"used\s*=\s*([\d.]+)M", out)
            _limit_cache["swap"] = int(float(m.group(1))) if m else None
        except Exception:
            _limit_cache["swap"] = None
    return _limit_cache["swap"]


def machine_state():
    """(free memory level in %, test workers already running)."""
    if "level" in _limit_cache:
        return _limit_cache["level"], _limit_cache["running"]
    try:
        level = int(subprocess.run(["sysctl", "-n", "kern.memorystatus_level"],
                                   capture_output=True, text=True, timeout=2).stdout)
    except Exception:
        level = None
    running = 0
    try:
        out = subprocess.run(["ps", "-axo", "rss=,args="], capture_output=True,
                             text=True, timeout=3).stdout
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) < 2 or int(parts[0]) < 200_000:
                continue
            args = parts[1]
            prog = os.path.basename(args.split()[0])
            if prog in ("node", "tsx", "ruby", "bun") and re.search(
                    r"jest|vitest|playwright|--test|parallel_tests|rails test|cdk|esbuild",
                    args):
                running += 1
    except Exception:
        pass
    _limit_cache.update(level=level, running=running)
    return level, running


def worker_limit():
    level, running = machine_state()
    if level is None:
        base = 2  # no data: conservative but usable
    elif level >= 60:
        base = 4
    elif level >= 40:
        base = 3
    elif level >= 20:
        base = 2
    else:
        base = 1
    swap = swap_used_mb()
    if swap is not None and swap >= SWAP_CAP_1_MB:
        base = 1
    elif swap is not None and swap >= SWAP_CAP_2_MB:
        base = min(base, 2)
    return max(1, min(base, MAX_PER_COMMAND, TOTAL_BUDGET - running))


# ---------------------------------------------------------------- parsing

def segments(cmd):
    """Split `cmd` into simple commands, outside quotes."""
    cmd = re.sub(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)",
                 "\n", cmd, flags=re.S)
    out, cur, quote, i = [], [], None, 0
    at_word_start = True
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(cmd):
                cur.append(cmd[i + 1])
                i += 1
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            cur.append(cmd[i:i + 2])
            i += 1
        elif ch == "#" and at_word_start:
            # comment: until end of line
            while i < len(cmd) and cmd[i] != "\n":
                i += 1
            continue
        elif ch in ";|&\n(){}`" or (ch == "$" and cmd[i + 1:i + 2] == "("):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        at_word_start = ch in " \t\n;|&(){}`"
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def split_tokens(seg):
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def strip_prefix(tokens, env):
    """Skip assignments, keywords and wrappers. Store the assignments in
    `env`. Return the tokens starting at the real command."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", t)
        if m:
            env[m.group(1)] = m.group(2)
            i += 1
        elif t in KEYWORDS or t in SIMPLE_WRAPPERS:
            i += 1
        elif t == "env":
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
        elif t in ("timeout", "gtimeout"):
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in ("-s", "-k", "--signal", "--kill-after") else 1
            i += 1  # duration
        elif t == "nice":
            i += 1
            if i < len(tokens) and tokens[i] == "-n":
                i += 2
            elif i < len(tokens) and re.match(r"^-\d+$", tokens[i]):
                i += 1
        elif t == "xargs":
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in ("-n", "-P", "-I", "-L", "-s", "-d", "-E") else 1
        else:
            break
    return tokens[i:]


def value(text, pattern):
    """Number of workers from a flag (accepts percentages). None if missing."""
    m = re.search(pattern, text)
    if not m:
        return None
    raw = m.group("v")
    if raw.endswith("%"):
        return max(1, -(-(os.cpu_count() or 10) * int(raw[:-1]) // 100))
    return int(raw)


NUM = r"(?P<v>\d+%?)"


def verdict(runner, got, fix, limit):
    if got is None:
        return f"{runner} has no concurrency limit -> add `{fix}`"
    if got > limit:
        return f"{runner} asks for {got} workers and the maximum right now is {limit} -> use `{fix}`"
    return None


# ---------------------------------------------------------------- runners

def check_runner(prog, args, env, cwd, limit):
    """`prog` is the executable in command position, `args` the rest."""
    text = " ".join(args)
    tokset = set(args)
    name = os.path.basename(prog)

    # node node_modules/jest/bin/jest.js, ./node_modules/.bin/vitest, etc.
    if name == "node":
        script = next((a for a in args if not a.startswith("-")), None)
        m = script and re.search(r"(?:^|/)(jest|vitest|playwright)(?:/|\.js|$)", script)
        if m:
            return check_runner(m.group(1), args[args.index(script) + 1:], env, cwd, limit)

    if tokset & INFO_FLAGS and name not in ("node", "tsx"):
        return None

    if name == "jest":
        if re.search(r"--runInBand|(^|\s)-i(\s|$)", text):
            return None
        return verdict("jest", value(text, r"(?:--maxWorkers|(?<!\S)-w)\s*=?\s*" + NUM),
                       f"--maxWorkers={limit}", limit)
    if name == "vitest":
        if re.search(r"--no-file-parallelism|singleFork|singleThread", text):
            return None
        got = value(text, r"(?:--maxWorkers|max(?:Forks|Threads))\s*=?\s*" + NUM)
        return verdict("vitest", got, f"--maxWorkers={limit}", limit)
    if name in ("tsx", "node") and "--test" in tokset:
        return verdict("`--test`", value(text, r"--test-concurrency\s*=?\s*" + NUM),
                       f"--test-concurrency={limit}", limit)
    if name == "playwright" and args[:1] == ["test"]:
        return verdict("playwright test",
                       value(text, r"(?:--workers|(?<!\S)-j)\s*=?\s*" + NUM),
                       f"--workers={limit}", limit)
    if name in ("parallel_rspec", "parallel_test"):
        return verdict("parallel_tests", value(text, r"(?<!\S)-n\s*=?\s*" + NUM),
                       f"-n {limit}", limit)
    if name == "turbo" and ("test" in tokset or any(a.startswith("test:") for a in args)):
        return verdict("turbo test (default: 10 tasks at a time)",
                       value(text, r"--concurrency\s*=?\s*" + NUM),
                       f"--concurrency={limit}", limit)
    if name == "nx" and re.search(r"run-many|affected", text) and re.search(r"\btest\b", text):
        return verdict("nx run-many test", value(text, r"--parallel\s*=?\s*" + NUM),
                       f"--parallel={limit}", limit)
    if name == "lerna" and args[:2] == ["run", "test"]:
        return verdict("lerna run test", value(text, r"--concurrency\s*=?\s*" + NUM),
                       f"--concurrency {limit}", limit)
    if name == "rails" and args and re.match(r"^test(:|$)", args[0]):
        try:
            with open(os.path.join(cwd, "test", "test_helper.rb")) as f:
                if "parallelize" not in f.read():
                    return None
        except OSError:
            return None
        got = env.get("PARALLEL_WORKERS")
        return verdict("rails test (with parallelize)",
                       int(got) if got and got.isdigit() else None,
                       f"PARALLEL_WORKERS={limit} bin/rails test", limit)
    return None


def package_script(cwd, name):
    try:
        with open(os.path.join(cwd, "package.json")) as f:
            return json.load(f).get("scripts", {}).get(name)
    except (OSError, ValueError):
        return None


def check_pkg_manager(pm, args, env, cwd, limit, depth):
    """npm/pnpm/yarn/bun: run a bin (exec/dlx/x) or run a script."""
    original = " ".join(args)
    args = list(args)
    recursive = False
    # global flags before the subcommand
    while args and args[0].startswith("-"):
        f = args.pop(0)
        if f in ("--prefix", "-C", "--dir", "--cwd") and args:
            d = os.path.expanduser(args.pop(0))
            cwd = d if os.path.isabs(d) else os.path.join(cwd, d)
        elif re.match(r"^--(prefix|dir|cwd)=", f):
            d = os.path.expanduser(f.split("=", 1)[1])
            cwd = d if os.path.isabs(d) else os.path.join(cwd, d)
        elif f in ("-r", "--recursive"):
            recursive = True
        elif f in ("--filter", "-F", "--workspace", "-w") and args:
            args.pop(0)
            recursive = True
        elif f.startswith("--filter=") or f.startswith("--workspace="):
            recursive = True
    if not args:
        return None
    sub, rest = args[0], args[1:]

    if (pm, sub) in (("pnpm", "exec"), ("pnpm", "dlx"), ("npm", "exec"), ("npm", "x"),
                     ("bun", "x"), ("yarn", "exec"), ("yarn", "dlx")):
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        rest = [r for r in rest if r != "--"]
        return check_runner(rest[0], rest[1:], env, cwd, limit) if rest else None

    if pm == "bun" and sub == "test":
        text = " ".join(rest)
        if re.search(r"--parallel\b", text):
            return verdict("bun --parallel", value(text, r"--parallel=" + NUM),
                           f"--parallel={limit}", limit)
        return None

    script_name, extra = None, rest
    if sub in ("test", "t", "tst"):
        script_name = "test"
    elif sub in ("run", "run-script"):
        rest = [r for r in rest if not r.startswith("-") or r == "--"]
        if rest:
            script_name, extra = rest[0], rest[1:]
    elif pm in ("yarn", "pnpm") and os.path.basename(sub) in RUNNERS:
        return check_runner(sub, rest, env, cwd, limit)
    elif pm in ("yarn", "pnpm") and package_script(cwd, sub):
        script_name = sub
    if not script_name:
        return None

    if recursive and pm == "pnpm" and re.match(r"^(test|t|tst)(:|$)", script_name):
        text = original
        got = value(text, r"--workspace-concurrency\s*=?\s*" + NUM)
        msg = verdict(f"recursive pnpm `{script_name}` (default: 4 packages at a time)",
                      got, f"--workspace-concurrency={limit}", limit)
        if msg:
            return msg
        # also check the script of the current package, if there is one

    script = package_script(cwd, script_name)
    if not script or depth >= 3:
        return None
    extra = [e for e in extra if e != "--"]
    combined = script + (" " + " ".join(shlex.quote(e) for e in extra) if extra else "")
    inner = analyze(combined, cwd, depth + 1, dict(env))
    if inner:
        sep = "without `--`" if pm in ("pnpm", "yarn", "bun") else "after `--`"
        return (f"`{pm} {sub}{' ' + script_name if sub in ('run', 'run-script') else ''}` "
                f"runs `{script}`: {inner[0]} (with {pm} the flag goes {sep})")
    return None


# ---------------------------------------------------------------- analysis

def analyze(cmd, cwd, depth=0, exported=None):
    """Return the list of problems found in `cmd`."""
    exported = dict(exported or {})
    problems = []
    for seg in segments(cmd):
        tokens = split_tokens(seg)
        if tokens[:1] == ["export"]:
            for t in tokens[1:]:
                if "=" in t:
                    k, v = t.split("=", 1)
                    exported[k] = v
            continue
        env = dict(exported)
        tokens = strip_prefix(tokens, env)
        if not tokens:
            continue
        prog, args = tokens[0], tokens[1:]
        name = os.path.basename(prog)

        if name in ("cd", "pushd") and args:
            d = os.path.expanduser(os.path.expandvars(args[0]))
            d = d if os.path.isabs(d) else os.path.join(cwd, d)
            if os.path.isdir(d):
                cwd = os.path.normpath(d)
            continue

        if name in SHELLS and depth < 3:
            for j, a in enumerate(args):
                if re.match(r"^-[a-z]*c[a-z]*$", a) and j + 1 < len(args):
                    problems += analyze(args[j + 1], cwd, depth + 1, env)
                    break
            continue

        if name == "bundle" and args[:1] == ["exec"] and len(args) > 1:
            prog, args = args[1], args[2:]
            name = os.path.basename(prog)

        if name in ("npx", "bunx", "pnpx"):
            while args and args[0].startswith("-"):
                f = args.pop(0)
                if f in ("-p", "--package") and args:
                    args.pop(0)
            if not args:
                continue
            rest = strip_prefix(args, env)
            if not rest:
                continue
            prog, args = rest[0], rest[1:]
            name = os.path.basename(prog)

        if name in PKG_MANAGERS:
            if machine_state_needed(args):
                msg = check_pkg_manager(name, args, env, cwd, get_limit(), depth)
                if msg:
                    problems.append(msg)
            continue

        if name in RUNNERS or prog.endswith(("bin/rails", "/jest", "/vitest")):
            msg = check_runner(prog, args, env, cwd, get_limit())
            if msg:
                problems.append(msg)
    return problems


def machine_state_needed(args):
    """Avoid computing the limit for `npm install`, `pnpm add`, etc."""
    for a in args:
        if a.startswith("-"):
            continue
        return a not in ("install", "i", "ci", "add", "remove", "rm", "uninstall",
                         "update", "up", "upgrade", "why", "ls", "list", "outdated",
                         "audit", "view", "info", "init", "create", "login", "publish",
                         "version", "link", "config", "cache", "prune", "dedupe")
    return False


def get_limit():
    if "limit" not in _limit_cache:
        _limit_cache["limit"] = worker_limit()
    return _limit_cache["limit"]


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    cmd = data.get("tool_input", {}).get("command", "") or ""
    cwd = data.get("cwd") or os.getcwd()
    problems = list(dict.fromkeys(analyze(cmd, cwd)))
    if not problems:
        return
    level, running = machine_state()
    limit = get_limit()
    state = (f"free memory {level}%" if level is not None else "free memory unknown")
    swap = swap_used_mb()
    if swap is not None:
        state += f", swap used {swap / 1024:.1f} GB"
    state += f", {running} test process{'es' if running != 1 else ''} already running"
    reason = (
        f"Blocked: test runner without a concurrency limit or above the maximum "
        f"({state}; maximum right now: {limit} worker{'s' if limit != 1 else ''}). "
        f"Too many parallel test workers can run the machine out of memory and freeze it.\n- "
        + "\n- ".join(problems)
        + "\nRun the command again with that limit."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the shell because of a bug in the hook
        try:
            import traceback
            with open(LOG, "a") as f:
                f.write(f"{e!r}\n{traceback.format_exc()}\n")
        except OSError:
            pass
