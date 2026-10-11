#!/usr/bin/env python3
"""Tests for hooks/limit-test-workers.py.

Run: python3 tests/test_limit_test_workers.py

The hook reads free memory, swap and running processes from the machine. To
make the tests repeatable, they load the hook as a module and preset its
internal cache, so the worker limit is always 2 here.
"""
import importlib.util
import json
import os
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "limit-test-workers.py")
LIMIT = 2


def load_hook():
    spec = importlib.util.spec_from_file_location("limit_test_workers", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._limit_cache.update(level=50, running=0, swap=0, limit=LIMIT)
    return mod


hook = load_hook()
pkg_dir = tempfile.mkdtemp()
with open(os.path.join(pkg_dir, "package.json"), "w") as f:
    json.dump({"scripts": {"test": "jest", "test:safe": "jest --maxWorkers=2"}}, f)

ALLOW = [
    "npx jest --maxWorkers=2",
    "npx jest --runInBand",
    "npx jest -i",
    "npx jest --version",
    "npx vitest run --maxWorkers=1",
    "npx tsx --test --test-concurrency=2 src/*.test.ts",
    "npx playwright test --workers=2",
    "pnpm -r --workspace-concurrency=2 test",
    "bun test",
    "bun test --parallel=2",
    "bundle exec parallel_rspec -n 2",
    "npm i -D jest",
    "grep -r jest src",
    "echo 'npx jest'",
    "cat <<'EOF'\nnpx jest\nEOF",
    "git log --grep jest",
    f"cd {pkg_dir} && npm run test:safe",
    f"cd {pkg_dir} && npm test -- --maxWorkers=2",
]

BLOCK = [
    "npx jest",
    "npx jest --maxWorkers=8",
    "npx vitest run",
    "npx tsx --test src/*.test.ts",
    "node --test",
    "npx playwright test",
    "pnpm -r test",
    "bun test --parallel",
    "bundle exec parallel_rspec",
    "bash -c \"npx jest\"",
    "FOO=1 timeout 60 npx jest",
    "ls && npx jest src",
    "node node_modules/jest/bin/jest.js",
    f"cd {pkg_dir} && npm test",
]

failures = 0
for expected, cases in (("allow", ALLOW), ("block", BLOCK)):
    for cmd in cases:
        problems = hook.analyze(cmd, os.getcwd())
        ok = (not problems) if expected == "allow" else bool(problems)
        if not ok:
            failures += 1
            print(f"FAIL ({expected}): {cmd!r}\n   {problems}")
print(f"{len(ALLOW) + len(BLOCK) - failures}/{len(ALLOW) + len(BLOCK)} ok")
sys.exit(1 if failures else 0)
