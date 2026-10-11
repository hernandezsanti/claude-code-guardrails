# claude-code-guardrails

Two small hooks for Claude Code. They run before every Bash command that the
agent wants to execute, and they block the command when it is risky.

- `protect_master.py` blocks git operations that change `master` or `main`.
- `limit-test-workers.py` blocks test runners that would start too many
  parallel workers for the memory that the machine has free right now.

Both hooks are plain Python 3 scripts with no dependencies.

## protect_master.py

### What it blocks

The hook blocks a Bash command when it would do one of these things:

- `git merge`, `git rebase` or `git reset` (when the reset moves the branch)
  while the current branch is `master` or `main`.
- `git push` to `master` or `main`. This includes an explicit refspec like
  `HEAD:main` or `+HEAD:refs/heads/master`, a push with no refspec while the
  current branch is `master` or `main`, and `git push origin --delete main`.
- `git push --all`, `--mirror` or `--branches`, because they include
  `master` and `main`.

When it blocks, it exits with code 2 and prints the reason to stderr. Claude
Code shows that reason to the agent.

### Why

An agent can merge, push or reset a production branch in a few seconds, and
that is hard to undo. These operations should happen only when the human
explicitly asks for them. The hook does not read the conversation, so it cannot
know whether the human authorized the operation. Because of that, it always
blocks, and the message asks the agent to let the human run the command
themselves. In Claude Code, the human can do that by typing the command with a
`!` prefix in the prompt. Commands run that way do not go through the hooks.

### How it decides

A simple text search for "master" or "main" blocks too much. For example, it
would block a push of a feature branch when the same command also runs
`git fetch origin master`, or when a PR body contains the word "main". This
hook works in a more precise way:

- It removes heredoc bodies and splits the command into segments on `&&`,
  `||`, `;`, `|`, `&` and newlines, outside quotes.
- It follows `cd`, `pushd`, `git -C <dir>`, `git checkout` and `git switch` in
  order, so it knows which directory and which branch each git command uses.
- It expands variables that are set in the same command, like
  `W=/path; git -C $W push`.
- It asks git for the current branch of each directory
  (`git rev-parse --abbrev-ref HEAD`).
- It skips common prefixes such as `env`, `VAR=value` and shell keywords such
  as `do` and `then`.

If the parser fails on a command, the hook falls back to a simple rule: it
blocks the command if it contains `push`, `merge`, `reset` or `rebase` and
also `master` or `main`.

## limit-test-workers.py

### What it blocks

The hook blocks a test command when it does not limit its concurrency, or when
it asks for more workers than the current limit. It knows these runners:

| Runner | Flag it expects |
|---|---|
| jest | `--maxWorkers=N` (or `--runInBand`) |
| vitest | `--maxWorkers=N` |
| `node --test`, `tsx --test` | `--test-concurrency=N` |
| playwright test | `--workers=N` |
| parallel_tests (`parallel_rspec`, `parallel_test`) | `-n N` |
| turbo test | `--concurrency=N` |
| nx run-many / affected test | `--parallel=N` |
| lerna run test | `--concurrency N` |
| rails test with `parallelize` | `PARALLEL_WORKERS=N` |
| bun test `--parallel` | `--parallel=N` |
| pnpm recursive test | `--workspace-concurrency=N` |

It also follows `npm test`, `npm run <script>`, `pnpm <script>`, `yarn` and
`bun` into `package.json`, including scripts that call other scripts, and it
looks inside `bash -c "..."`. It only looks at the runner in command position,
so `npm i -D jest` or `grep jest` are not blocked.

When it blocks, it returns a JSON `deny` decision. The reason tells the agent
the exact value of N to use, so the agent can run the command again with the
right flag.

### Why

Many test runners start one worker per CPU core by default. Each worker can use
around 1.5 GB of RAM. On a 16 GB machine, one `npx jest` with no limit can use
all the memory, and when several agents run tests at the same time, the
machine can freeze. The hook keeps the total number of test workers under
control without changing any project configuration, which other developers
and CI also use.

### How it computes the limit

- It reads the free memory level from macOS (`sysctl kern.memorystatus_level`):
  60% or more gives 4 workers, 40% or more gives 3, 20% or more gives 2, and
  less gives 1.
- It reads the used swap (`sysctl vm.swapusage`). With 2 GB or more of swap the
  maximum is 2, and with 4 GB or more it is 1.
- It counts large test processes that are already running (node, tsx, ruby or
  bun processes over 200 MB that look like test runners) and subtracts them
  from a total budget of 6 workers.
- The result is always between 1 and 4.

You can change the total budget with the `CLAUDE_TEST_WORKER_BUDGET`
environment variable.

## Install

1. Copy the hooks to your Claude Code hooks folder:

   ```sh
   mkdir -p ~/.claude/hooks
   cp hooks/protect_master.py hooks/limit-test-workers.py ~/.claude/hooks/
   ```

2. Add the hook entries from `examples/settings.json` to the `hooks` section
   of `~/.claude/settings.json`. If you already have a `PreToolUse` list,
   add the two entries to it instead of replacing it.

3. Start a new Claude Code session. The hooks apply to every Bash command.

`limit-test-workers.py` uses `/usr/bin/python3` on purpose, because a Python
version manager shim (like pyenv) can add almost half a second to every Bash
command. If your system Python is somewhere else, change the path in
`settings.json`.

## Run the tests

The tests do not need any package. Run them from the repository root:

```sh
python3 tests/test_protect_master.py
python3 tests/test_limit_test_workers.py
```

`test_protect_master.py` uses the `PROTECT_MASTER_BRANCHES` environment
variable to tell the hook which branch each fake directory is on, so it does
not need real repositories. `test_limit_test_workers.py` sets the worker limit
to 2, so the result does not depend on the memory of your machine.

## Limitations

- `protect_master.py` only protects branches named `master` and `main`.
- It does not see git aliases, scripts that call git inside them, or
  operations through other tools such as `gh pr merge` or the GitHub API.
- With `push.default=upstream`, a plain `git push` from a local branch with
  another name that tracks `main` is not blocked.
- The fallback rule for commands that the parser cannot read is simple, and
  it only blocks when the command names `master` or `main`.
- `protect_master.py` cannot detect that the human authorized an operation.
  It always blocks, and the human has to run the command or disable the hook.
- `limit-test-workers.py` reads memory and swap with macOS `sysctl` keys. On
  other systems these reads fail and the limit falls back to 2 workers.
- If `limit-test-workers.py` hits an unexpected error, it lets the command run
  and writes the error to `~/.claude/hooks/limit-test-workers.log`. It never
  blocks a command because of its own bug.
- The two hooks block in different ways: `protect_master.py` uses exit code 2
  and stderr, and `limit-test-workers.py` uses a JSON `deny` decision.
- Both hooks parse shell commands with their own simple parser, not a full
  shell. Unusual quoting or very dynamic commands can get past them.

## License

MIT. See `LICENSE`.
