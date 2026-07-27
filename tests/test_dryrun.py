"""AC2: dry-run entrypoint prints and validates the in-container command line."""

from __future__ import annotations

import json

from sidebutton_harbor_agent.dryrun import main


def test_dryrun_human_output_and_exit_zero(capsys) -> None:
    rc = main(
        ["--model", "anthropic/claude-opus-4-8", "--effort", "high", "--api-key", "sk-x"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "claude --verbose --output-format=stream-json" in out
    assert "--effort high" in out
    assert "ANTHROPIC_MODEL=claude-opus-4-8" in out
    assert "ANTHROPIC_API_KEY=sk-x" in out
    assert "dry-run OK" in out


def test_dryrun_json_output_is_valid_and_clean(tmp_path, capsys) -> None:
    # Cold arm driven from an explicit empty packs dir rather than the bundled
    # one: this assertion is about what a *cold* invocation looks like, and it
    # has to keep holding the day a real export lands in packs/.
    empty = tmp_path / "no-packs"
    empty.mkdir()
    rc = main(
        ["--model", "anthropic/claude-opus-4-8", "--effort", "max", "--json",
         "--packs-dir", str(empty)]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"] == "sidebutton"
    assert payload["version"].startswith("0.2.0+cli.")
    assert payload["env"]["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert "--effort max" in payload["cli_flags"]
    assert payload["command"].startswith("claude ")
    assert payload["packs"] == []  # cold arm

    # Fairness: no verifier/timeout/resource override anywhere in the invocation.
    blob = f"{payload['command']} {payload['cli_flags']} {' '.join(payload['setup_commands'])}"
    for forbidden in ("--verifier", "--timeout", "--cpus", "--memory", "--gpus"):
        assert forbidden not in blob


def test_dryrun_default_model(capsys) -> None:
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ANTHROPIC_MODEL=claude-opus-4-8" in out
