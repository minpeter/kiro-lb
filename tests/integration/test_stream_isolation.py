import json
import os
import subprocess
import sys
from pathlib import Path


def test_parallel_streams_are_isolated() -> None:
    repository = Path(__file__).parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository)

    completed = subprocess.run(
        [
            sys.executable,
            str(
                repository
                / "tests"
                / "integration"
                / "parser_integrity_probe.py"
            ),
        ],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "anthropic_unicode": True,
        "openai_unicode": True,
        "anthropic_tool_ids": True,
        "openai_tool_ids": True,
        "anthropic_malformed_clean": False,
        "openai_malformed_clean": False,
        "anthropic_chronology": True,
        "anthropic_chronology_tools": True,
        "anthropic_chronology_signatures": True,
        "parallel_isolated": True,
    }
