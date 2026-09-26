"""Contract tests for scripts/dev.sh, the per-worktree local development stack.

The script is what keeps a dev server off the production store and secrets, so
its refusals are tested as behaviour, not read as prose: a real throwaway git
repository with linked worktrees stands in for the checkout layout. Nothing here
starts a server or touches the network; `api`/`web` are only driven far enough
to hit their guards.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "dev.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None or shutil.which("openssl") is None,
    reason="dev.sh needs bash, git and openssl",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _run(tree: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # DEV_HOST is pinned so the tests never depend on the host's routing table,
    # and a stub `ss` reports nothing listening so they never depend on which
    # ports the host happens to have open (a live dev server holds slot 0).
    shim = next(parent / "shim-bin" for parent in tree.parents if (parent / "shim-bin").is_dir())
    run_env = {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}", "HOME": str(tree), "DEV_HOST": "192.0.2.1"}
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(tree / "scripts" / "dev.sh"), *args],
        cwd=tree,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _env_value(path: Path, key: str) -> str | None:
    match = re.search(rf'^{key}="([^"]*)"$', path.read_text(), re.MULTILINE)
    return match.group(1) if match else None


@pytest.fixture
def layout(tmp_path: Path):
    """A main checkout plus a factory for sibling worktrees, like the real host."""
    shim = tmp_path / "shim-bin"
    shim.mkdir()
    (shim / "ss").write_text("#!/bin/sh\nexit 0\n")
    (shim / "ss").chmod(0o755)
    main = tmp_path / "kiro-lb"
    (main / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, main / "scripts" / "dev.sh")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "add", ".")
    _git(main, "commit", "-q", "-m", "init")
    worktrees = tmp_path / "kiro-lb-worktrees"

    def add(name: str) -> Path:
        path = worktrees / name
        _git(main, "worktree", "add", "-q", str(path), "-b", f"test/{name}")
        return path

    return main, add


def test_script_is_executable_and_self_documenting():
    assert _SCRIPT.stat().st_mode & stat.S_IXUSR
    result = subprocess.run(["bash", str(_SCRIPT), "--help"], capture_output=True, text=True, check=True)
    for command in ("init", "api", "web", "status"):
        assert f"scripts/dev.sh {command}" in result.stdout


def test_script_hardcodes_no_address():
    # The bind address is derived at run time; a literal IP would pin the
    # script to one network. Loopback and wildcard are banned by name too.
    text = _SCRIPT.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", code)


def test_init_writes_private_dev_env_with_slot_zero(layout):
    _main, add = layout
    tree = add("one")
    result = _run(tree, "init", "--no-deps")
    assert result.returncode == 0, result.stderr

    env_file = tree / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert _env_value(env_file, "DEV_API_PORT") == "8100"
    assert _env_value(env_file, "DEV_WEB_PORT") == "5174"
    assert (_env_value(env_file, "PROXY_API_KEY") or "").startswith("dev-")
    assert (_env_value(env_file, "DASHBOARD_PASSWORD") or "").startswith("dev-")
    # pytest reads this .env too; these would break the default-value tests.
    text = env_file.read_text()
    assert "SERVER_HOST" not in text and "SERVER_PORT" not in text
    assert "KIRO_SLOT" not in text and "HANDOFF_SECRET" not in text


def test_rerunning_init_keeps_secrets_and_slot(layout):
    _main, add = layout
    tree = add("one")
    assert _run(tree, "init", "--no-deps").returncode == 0
    before = (tree / ".env").read_text()
    result = _run(tree, "init", "--no-deps")
    assert result.returncode == 0, result.stderr
    assert (tree / ".env").read_text() == before


def test_each_worktree_gets_its_own_slot_and_freed_slots_are_reused(layout):
    main, add = layout
    first, second = add("one"), add("two")
    assert _run(first, "init", "--no-deps").returncode == 0
    assert _run(second, "init", "--no-deps").returncode == 0
    assert _env_value(first / ".env", "DEV_API_PORT") == "8100"
    assert _env_value(second / ".env", "DEV_API_PORT") == "8110"
    assert _env_value(second / ".env", "DEV_WEB_PORT") == "5184"

    _git(main, "worktree", "remove", "--force", str(first))
    third = add("three")
    assert _run(third, "init", "--no-deps").returncode == 0
    assert _env_value(third / ".env", "DEV_API_PORT") == "8100"


def test_init_skips_a_slot_whose_port_is_already_listening(layout, tmp_path):
    # Something unrelated to any worktree holds 5174: slot 0 must be skipped
    # even though no .env claims it.
    _main, add = layout
    (tmp_path / "shim-bin" / "ss").write_text(
        '#!/bin/sh\ncase "$*" in *":5174 "*) echo "LISTEN 0 511 192.0.2.1:5174 0.0.0.0:*";; esac\n'
    )
    tree = add("one")
    assert _run(tree, "init", "--no-deps").returncode == 0
    assert _env_value(tree / ".env", "DEV_API_PORT") == "8110"


def test_concurrent_inits_never_share_a_slot(layout):
    _main, add = layout
    trees = [add(f"w{i}") for i in range(4)]
    with ThreadPoolExecutor(max_workers=len(trees)) as pool:
        results = list(pool.map(lambda tree: _run(tree, "init", "--no-deps"), trees))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    ports = [_env_value(tree / ".env", "DEV_API_PORT") for tree in trees]
    assert sorted(ports) == ["8100", "8110", "8120", "8130"]


@pytest.mark.parametrize("command", ["init", "api", "web"])
def test_every_command_refuses_the_deploy_root(layout, command):
    main, _add = layout
    (main / "deploy" / "bluegreen").mkdir(parents=True)
    (main / "deploy" / "bluegreen" / "active_slot").write_text("green\n")
    result = _run(main, command)
    assert result.returncode != 0
    assert "deploy root" in result.stderr
    assert not (main / ".env").exists()


@pytest.mark.parametrize("command", ["api", "web"])
def test_servers_refuse_an_env_without_dev_ports(layout, command):
    # A production .env has no DEV_ lines; starting against it would serve the
    # production key from a dev process.
    _main, add = layout
    tree = add("one")
    (tree / ".env").write_text('PROXY_API_KEY="production"\n')
    result = _run(tree, command)
    assert result.returncode != 0
    assert "not a dev .env" in result.stderr


def test_servers_require_init_first(layout):
    _main, add = layout
    tree = add("one")
    result = _run(tree, "api")
    assert result.returncode != 0
    assert "scripts/dev.sh init" in result.stderr


def test_status_lists_every_worktree_slot(layout):
    _main, add = layout
    first, second = add("one"), add("two")
    assert _run(first, "init", "--no-deps").returncode == 0
    assert _run(second, "init", "--no-deps").returncode == 0
    result = _run(first, "status")
    assert result.returncode == 0, result.stderr
    assert str(first) in result.stdout and str(second) in result.stdout
    assert "http://192.0.2.1:5174" in result.stdout


def test_unknown_command_fails():
    result = subprocess.run(["bash", str(_SCRIPT), "bogus"], capture_output=True, text=True)
    assert result.returncode != 0
