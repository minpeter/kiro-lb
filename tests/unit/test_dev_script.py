"""Contract tests for scripts/dev.sh, the per-worktree local development stack.

The script is what keeps a dev server off the production store and secrets, so
its refusals are tested as behaviour, not read as prose: a real throwaway git
repository with linked worktrees stands in for the checkout layout, and a stub
`portless` records how the script drives it. Nothing here starts a server,
opens a port or touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "dev.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None or shutil.which("openssl") is None,
    reason="dev.sh needs bash, git and openssl",
)

# Records every call as one JSON line, prints a plausible URL for `get`, and
# for `run` records the environment the child would inherit instead of
# starting it.
_STUB_PORTLESS = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["STUB_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": {
        k: v for k, v in os.environ.items()
        if k.startswith(("PORTLESS_", "API_PROXY_")) or k in ("PROXY_API_KEY", "DASHBOARD_PASSWORD")
    }}) + "\\n")
if sys.argv[1:2] == ["get"]:
    print(f"http://wt.{sys.argv[2]}.local:{os.environ['PORTLESS_PORT']}")
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def layout(tmp_path: Path):
    """A main checkout plus a factory for sibling worktrees, like the real host."""
    main = tmp_path / "kiro-lb"
    (main / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, main / "scripts" / "dev.sh")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "add", ".")
    _git(main, "commit", "-q", "-m", "init")
    stub = tmp_path / "portless"
    stub.write_text(_STUB_PORTLESS)
    stub.chmod(0o755)
    log = tmp_path / "portless.log"

    def add(name: str, *, ready: bool = False) -> Path:
        path = tmp_path / "kiro-lb-worktrees" / name
        _git(main, "worktree", "add", "-q", str(path), "-b", f"test/{name}")
        if ready:
            # What `init` would install, without installing it.
            (path / ".venv" / "bin").mkdir(parents=True)
            python = path / ".venv" / "bin" / "python"
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            (path / "frontend" / "node_modules").mkdir(parents=True)
        return path

    def run(tree: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        run_env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            # Pinned so the tests never depend on the host's routing table.
            "DEV_HOST": "192.0.2.1",
            "DEV_PORTLESS_BIN": str(stub),
            "STUB_LOG": str(log),
            # A production key exported in the caller's shell, as on the real host.
            "PROXY_API_KEY": "production-key",
        }
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

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return main, add, run, calls


def test_script_is_executable_and_self_documenting():
    assert _SCRIPT.stat().st_mode & stat.S_IXUSR
    result = subprocess.run(["bash", str(_SCRIPT), "--help"], capture_output=True, text=True, check=True)
    for command in ("init", "api", "web", "status", "proxy-stop"):
        assert f"scripts/dev.sh {command}" in result.stdout


def test_script_hardcodes_no_address():
    # The advertised address is derived at run time; a literal IP would pin
    # the script to one network.
    code = "\n".join(line for line in _SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", code)


def test_init_writes_private_marked_dev_env(layout):
    _main, add, run, _calls = layout
    tree = add("one")
    result = run(tree, "init", "--no-deps")
    assert result.returncode == 0, result.stderr

    env_file = tree / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    text = env_file.read_text()
    assert re.search(r'^DEV_ENV="1"$', text, re.MULTILINE)
    assert re.search(r'^PROXY_API_KEY="dev-[0-9a-f]{32}"$', text, re.MULTILINE)
    assert re.search(r'^DASHBOARD_PASSWORD="dev-[0-9a-f]{16}"$', text, re.MULTILINE)
    # pytest reads this .env too; these would break the default-value tests.
    for forbidden in ("SERVER_HOST", "SERVER_PORT", "KIRO_SLOT", "HANDOFF_SECRET"):
        assert forbidden not in text


def test_each_init_generates_distinct_secrets(layout):
    _main, add, run, _calls = layout
    first, second = add("one"), add("two")
    assert run(first, "init", "--no-deps").returncode == 0
    assert run(second, "init", "--no-deps").returncode == 0
    assert (first / ".env").read_text() != (second / ".env").read_text()


def test_rerunning_init_keeps_secrets(layout):
    _main, add, run, _calls = layout
    tree = add("one")
    assert run(tree, "init", "--no-deps").returncode == 0
    before = (tree / ".env").read_text()
    result = run(tree, "init", "--no-deps")
    assert result.returncode == 0, result.stderr
    assert (tree / ".env").read_text() == before


@pytest.mark.parametrize("command", ["init", "api", "web"])
def test_every_command_refuses_the_deploy_root(layout, command):
    main, _add, run, calls = layout
    (main / "deploy" / "bluegreen").mkdir(parents=True)
    (main / "deploy" / "bluegreen" / "active_slot").write_text("green\n")
    result = run(main, command)
    assert result.returncode != 0
    assert "deploy root" in result.stderr
    assert not (main / ".env").exists()
    assert calls() == []


@pytest.mark.parametrize("command", ["init", "api", "web"])
def test_commands_refuse_an_env_without_the_dev_marker(layout, command):
    # A production .env has no DEV_ENV line; serving it from a dev process
    # would accept the production key.
    _main, add, run, calls = layout
    tree = add("one", ready=True)
    (tree / ".env").write_text('PROXY_API_KEY="production"\n')
    result = run(tree, command)
    assert result.returncode != 0
    assert "not a dev .env" in result.stderr
    assert calls() == []


def test_servers_require_init_first(layout):
    _main, add, run, calls = layout
    tree = add("one")
    result = run(tree, "api")
    assert result.returncode != 0
    assert "scripts/dev.sh init" in result.stderr
    assert calls() == []


def test_api_runs_behind_a_lan_proxy_with_a_scrubbed_environment(layout):
    _main, add, run, calls = layout
    tree = add("one", ready=True)
    assert run(tree, "init", "--no-deps").returncode == 0
    result = run(tree, "api")
    assert result.returncode == 0, result.stderr

    start, *_, launch = calls()
    assert start["argv"][:2] == ["proxy", "start"]
    assert "--lan" in start["argv"] and "--no-tls" in start["argv"]
    assert start["argv"][start["argv"].index("--ip") + 1] == "192.0.2.1"
    # Own state dir and port: a portless setup other projects use is untouched,
    # and /etc/hosts is never edited.
    assert start["env"]["PORTLESS_STATE_DIR"].endswith(".portless-kiro-lb")
    assert start["env"]["PORTLESS_PORT"] == "1356"
    assert start["env"]["PORTLESS_SYNC_HOSTS"] == "0"

    argv = launch["argv"]
    assert argv[:3] == ["run", "--name", "api.kiro-lb"]
    inner = argv[argv.index("-c") + 1]
    # env -i is what stops the exported production key reaching main.py.
    assert inner.startswith("exec env -i ")
    assert '--host "$HOST" --port "$PORT"' in inner
    assert argv[-1] == str(tree / ".venv" / "bin" / "python")


def test_web_routes_the_api_through_the_proxy_by_host_header(layout):
    _main, add, run, calls = layout
    tree = add("one", ready=True)
    assert run(tree, "init", "--no-deps").returncode == 0
    result = run(tree, "web")
    assert result.returncode == 0, result.stderr

    launch = calls()[-1]
    assert launch["argv"][:3] == ["run", "--name", "kiro-lb"]
    assert launch["cwd"] == str(tree / "frontend")
    # Vite targets the proxy on loopback and names the api route, so an api
    # restart (new port) never breaks the dashboard's proxy.
    assert launch["env"]["API_PROXY_TARGET"] == "http://localhost:1356"
    assert launch["env"]["API_PROXY_HOST"] == "wt.api.kiro-lb.local:1356"


def test_proxy_port_and_state_dir_are_overridable(layout, tmp_path):
    _main, add, run, calls = layout
    tree = add("one", ready=True)
    assert run(tree, "init", "--no-deps").returncode == 0
    override = {"DEV_PROXY_PORT": "1400", "DEV_PORTLESS_STATE_DIR": str(tmp_path / "state")}
    assert run(tree, "api", env=override).returncode == 0
    start = calls()[0]
    assert start["argv"][start["argv"].index("-p") + 1] == "1400"
    assert start["env"]["PORTLESS_STATE_DIR"] == str(tmp_path / "state")


def test_status_prints_this_worktrees_urls(layout):
    _main, add, run, _calls = layout
    tree = add("one", ready=True)
    assert run(tree, "init", "--no-deps").returncode == 0
    result = run(tree, "status")
    assert result.returncode == 0, result.stderr
    assert "dashboard http://wt.kiro-lb.local:1356" in result.stdout
    assert "api http://wt.api.kiro-lb.local:1356" in result.stdout


def test_unknown_command_fails():
    result = subprocess.run(["bash", str(_SCRIPT), "bogus"], capture_output=True, text=True)
    assert result.returncode != 0
