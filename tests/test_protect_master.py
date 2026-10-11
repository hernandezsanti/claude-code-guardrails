#!/usr/bin/env python3
"""Regression tests for hooks/protect_master.py.

Run: python3 tests/test_protect_master.py

The "allow" cases are false positives that a naive text-matching version of
the hook used to block. The "block" cases are real and synthetic ways of
changing master/main. Repository paths and branch names are fictional: the
PROTECT_MASTER_BRANCHES variable tells the hook which branch each directory is
on, so no real repository is needed.
"""
import json
import os
import subprocess
import sys

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "protect_master.py")
H = os.path.expanduser("~")
BRANCHES = {
    f"{H}/worktrees/webapp/DOC-1-agent-nav": "fix/DOC-1-agent-navigation-docs",
    f"{H}/worktrees/webapp/SEO-12-check": "feature/SEO-12-mcp",
    f"{H}/worktrees/student-app/BUG-990-hotfix": "hotfix/BUG-990-error-tracking",
    f"{H}/worktrees/cms/BUG-990-prod-master": "hotfix/BUG-990-error-tracking-prod-master",
    f"{H}/code/teachers-app": "master",
    f"{H}/worktrees/teachers-app/BUG-990-sm-prod": "master",
    f"{H}/worktrees/file-browser/OPS-825": "feature/OPS-825-cdn",
    f"{H}/code/api-server": "main",
    f"{H}/worktrees/api-server/API-920-db-role": "feature/API-920-db-role",
    f"{H}/code/infra": "main",
    f"{H}/worktrees/cli-tool/release-0.3.16": "release/0.3.16",
}

ALLOW = [
    "cd ~/worktrees/webapp/DOC-1-agent-nav && git push -u origin fix/DOC-1-agent-navigation-docs 2>&1 | tail -2 && cat > /tmp/pr-body.md <<'EOF'\n- Adds a CLAUDE.md so agents know the main entry points\nEOF\ngh pr create --base master --body-file /tmp/pr-body.md",
    "cd ~/worktrees/student-app/BUG-990-hotfix && git add src/App.vue && git commit -q -F - <<'EOF'\n[BUG-990] Point error tracking to the new server on master\nEOF\ngit push -q -u origin hotfix/BUG-990-error-tracking",
    "git -C ~/worktrees/cms/BUG-990-prod-master push -q -u origin hotfix/BUG-990-error-tracking-prod-master 2>&1 | grep -v '^remote:'",
    "cd ~/worktrees/cms/BUG-990-prod-master && git branch -m hotfix/BUG-990-error-tracking-cms-prod && git push -q -u origin hotfix/BUG-990-error-tracking-cms-prod",
    "cd ~/code/teachers-app && git fetch -q origin master && git worktree add -q ~/worktrees/teachers-app/BUG-990-sm-prod -b hotfix/BUG-990-teachers-sourcemaps origin/master && cd ~/worktrees/teachers-app/BUG-990-sm-prod && git cherry-pick abc1234 && git push -q -u origin hotfix/BUG-990-teachers-sourcemaps",
    "cd ~/worktrees/file-browser/OPS-825 && git add template.yaml && git commit -q -m \"[OPS-825] Invalidate the master distribution\" && git push -q -u origin feature/OPS-825-cdn",
    "cd ~/worktrees/api-server/API-920-db-role && git add -A && git commit -qm \"[API-920] deploy_prod uses main\" && git push -u origin feature/API-920-db-role",
    "git -C ~/code/api-server push -q origin 306f6df:refs/heads/fix/API-196-public-service",
    "cd ~/worktrees/webapp/SEO-12-check && git checkout -q -b feature/SEO-12-integration && git fetch -q origin && git merge --no-ff origin/master -m \"[SEO-12] Merge master into the MCP branch\"",
    "cd ~/code/teachers-app && git merge-base --is-ancestor origin/master HEAD && echo up to date",
    "cd ~/code/infra && git stash push -m wip && git status",
    "cd ~/code/infra && git reset HEAD app/db.py",
    "cd ~/code/infra && git merge --abort",
    "cd ~/code/infra && git log --oneline origin/main..HEAD && echo 'push to main later'",
    f"W={H}/code/api-server; git -C $W push -q origin 306f6df:refs/heads/fix/API-196-x",
]

BLOCK = [
    "cd ~/worktrees/cli-tool/release-0.3.16 && git fetch -q origin && git push origin HEAD:main 2>&1 | tail -3",
    "git -C ~/code/infra merge --no-ff fix/short-notice",
    "cd ~/code/infra && git checkout -q main && git merge --no-ff -q fix/short-notice -m \"Merge\"",
    "cd ~/worktrees/webapp/DOC-1-agent-nav && git checkout -q main && for b in a b; do git merge -q --no-ff --no-edit $b || break; done",
    "cd ~/worktrees/webapp/DOC-1-agent-nav && git push origin main",
    "cd ~/worktrees/webapp/DOC-1-agent-nav && git push -f origin +HEAD:refs/heads/master",
    "cd ~/code/infra && git push",
    "cd ~/code/infra && git push origin HEAD",
    "cd ~/code/infra && git push origin",
    "cd ~/worktrees/webapp/DOC-1-agent-nav && git push --all origin",
    "cd ~/code/infra && git reset --hard origin/main",
    "cd ~/code/infra && git rebase origin/feature/x",
    "cd ~/worktrees/webapp/DOC-1-agent-nav && git push origin --delete main",
    "cd ~/worktrees/webapp/DOC-1-agent-nav && rtk git push origin main",
    f"W={H}/code/infra; git -C $W push origin HEAD",
    "R=~/code/infra && git -C \"$R\" merge feature/x",
]


def run(cmd):
    env = dict(os.environ, PROTECT_MASTER_BRANCHES=json.dumps(BRANCHES))
    p = subprocess.run([sys.executable, HOOK], input=json.dumps({"tool_input": {"command": cmd}, "cwd": H}),
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stderr.strip()


failures = 0
for expected, cases in (("allow", ALLOW), ("block", BLOCK)):
    for cmd in cases:
        rc, err = run(cmd)
        ok = (rc == 0) if expected == "allow" else (rc == 2)
        if not ok:
            failures += 1
            print(f"FAIL ({expected}, rc={rc}): {cmd[:110]!r}\n   {err[:200]}")
print(f"{len(ALLOW) + len(BLOCK) - failures}/{len(ALLOW) + len(BLOCK)} ok")
sys.exit(1 if failures else 0)
