"""Deploy guard: make "running code != deployed code" impossible to miss.

Adapted from a prior agent stack's deploy guard (verified). Generalized: repo root
is a parameter, no daemon/launchd specifics. The critical use here is
verification_preflight() in the credential-gated live-integration smoke
suites — it refuses to let a "live test passed" claim stand against stale code.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=repo, capture_output=True,
                          text=True, timeout=10)
    return proc.stdout.strip() if proc.returncode == 0 else ''


@dataclass(frozen=True)
class StartupSnapshot:
    loaded_commit: str
    loaded_at: str
    pid: int
    repo_root: str


def record_startup_commit(repo_root: Path, pid: int, now_iso: str,
                          state_path: Path) -> StartupSnapshot:
    """Snapshot HEAD + pid at process start. now_iso/pid injected (no clock in
    lib). Idempotent per state_path."""
    snap = StartupSnapshot(loaded_commit=_git(repo_root, 'rev-parse', 'HEAD'),
                           loaded_at=now_iso, pid=pid,
                           repo_root=str(repo_root))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(snap.__dict__, indent=2))
    return snap


@dataclass(frozen=True)
class StaleCheck:
    stale: bool
    loaded_commit: str | None
    current_head: str | None
    message: str


def check_for_stale_code(repo_root: Path, state_path: Path) -> StaleCheck:
    """Compare recorded startup commit against current HEAD."""
    if not state_path.exists():
        return StaleCheck(False, None, None, 'no startup snapshot recorded')
    loaded = json.loads(state_path.read_text()).get('loaded_commit')
    head = _git(repo_root, 'rev-parse', 'HEAD')
    if loaded and head and loaded != head:
        return StaleCheck(True, loaded, head,
                          f'running code {loaded[:8]} != HEAD {head[:8]} — '
                          f'restart before trusting live results')
    return StaleCheck(False, loaded, head, 'running code matches HEAD')


@dataclass(frozen=True)
class Preflight:
    should_block: bool
    message: str


def verification_preflight(repo_root: Path, state_path: Path) -> Preflight:
    """For live-integration smokes: block if the running process predates the
    current HEAD (whatever you're about to verify was written after start)."""
    chk = check_for_stale_code(repo_root, state_path)
    if chk.stale:
        return Preflight(True, f'[VERIFICATION BLOCKED] {chk.message}')
    # Also block on a dirty tree (uncommitted code under verification).
    # Parse the path column-independently: _git strips output, so the
    # porcelain status prefix width is unreliable — take the last token.
    porcelain = _git(repo_root, 'status', '--porcelain')
    dirty = []
    for line in porcelain.splitlines():
        parts = line.split()
        path = parts[-1] if parts else ''
        if path.startswith(('src/', 'tests/', 'scripts/')):
            dirty.append(path)
    if dirty:
        return Preflight(True, f'[VERIFICATION BLOCKED] {len(dirty)} '
                              f'uncommitted change(s) under test')
    return Preflight(False, 'preflight clean')
