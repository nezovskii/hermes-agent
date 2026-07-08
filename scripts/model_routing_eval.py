#!/usr/bin/env python3
"""Run or dry-run model routing evals for fast coding workers.

The live path intentionally runs each case in an isolated git worktree so a
fast worker model can edit freely without touching the developer checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_MODELS = [
    "openai-codex:gpt-5.3-codex-spark",
    "openai-codex:gpt-5.5",
]


def run(cmd: list[str] | str, *, cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    if isinstance(cmd, str):
        return subprocess.run(cmd, cwd=cwd, shell=True, text=True, capture_output=True, timeout=timeout)
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)


def repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parents[1]


def load_pack(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open() as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            required = {"id", "prompt", "verifiers"}
            missing = required - row.keys()
            if missing:
                raise SystemExit(f"{path}:{line_no}: missing keys: {sorted(missing)}")
            if not isinstance(row["verifiers"], list) or not all(isinstance(v, str) for v in row["verifiers"]):
                raise SystemExit(f"{path}:{line_no}: verifiers must be a list of shell command strings")
            cases.append(row)
    if not cases:
        raise SystemExit(f"{path}: no cases loaded")
    return cases


def parse_model(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("model must be provider:model, e.g. openai-codex:gpt-5.3-codex-spark")
    provider, model = value.split(":", 1)
    if not provider or not model:
        raise argparse.ArgumentTypeError("model must be provider:model")
    return provider, model


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def git_diff_stats(cwd: Path) -> dict[str, Any]:
    stat = run(["git", "diff", "--stat"], cwd=cwd)
    numstat = run(["git", "diff", "--numstat"], cwd=cwd)
    changed_files = 0
    added = 0
    deleted = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            changed_files += 1
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                deleted += int(parts[1])
    return {
        "changed_files": changed_files,
        "added_lines": added,
        "deleted_lines": deleted,
        "stat": stat.stdout.strip(),
    }


def create_worktree(root: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = run(["git", "worktree", "add", "--detach", str(dest), "HEAD"], cwd=root, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def remove_worktree(root: Path, dest: Path) -> None:
    run(["git", "worktree", "remove", "--force", str(dest)], cwd=root, timeout=120)


def live_case(root: Path, out_dir: Path, case: dict[str, Any], provider: str, model: str, timeout: int) -> dict[str, Any]:
    case_id = case["id"]
    worktree = out_dir / "worktrees" / safe_name(case_id) / safe_name(f"{provider}_{model}")
    create_worktree(root, worktree)
    started = time.time()
    prompt = (
        f"You are running a model-routing coding eval. Work inside this repo only.\n"
        f"Case: {case_id}\n"
        f"Task: {case['prompt']}\n"
        f"After changes, run the specified verifier commands if practical. Keep output concise."
    )
    hermes_cmd = [
        "hermes",
        "--worktree",
        "chat",
        "--provider",
        provider,
        "-m",
        model,
        "-t",
        "terminal,file",
        "-q",
        prompt,
    ]
    try:
        agent = run(hermes_cmd, cwd=worktree, timeout=timeout)
        agent_exit_code = agent.returncode
        agent_stdout = agent.stdout
        agent_stderr = agent.stderr
    except subprocess.TimeoutExpired as exc:
        agent_exit_code = 124
        agent_stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        agent_stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        agent_stderr += f"\nTimed out after {timeout}s"
    verifier_rows = []
    for verifier in case["verifiers"]:
        try:
            proc = run(verifier, cwd=worktree, timeout=timeout)
            verifier_rows.append({
                "command": verifier,
                "exit_code": proc.returncode,
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-2000:],
            })
        except subprocess.TimeoutExpired as exc:
            verifier_rows.append({"command": verifier, "exit_code": 124, "stdout_tail": str(exc), "stderr_tail": "timeout"})
    duration = round(time.time() - started, 2)
    return {
        "case_id": case_id,
        "category": case.get("category"),
        "provider": provider,
        "model": model,
        "duration_seconds": duration,
        "agent_exit_code": agent_exit_code,
        "agent_stdout_tail": agent_stdout[-4000:],
        "agent_stderr_tail": agent_stderr[-4000:],
        "verifiers": verifier_rows,
        "diff": git_diff_stats(worktree),
        "worktree": str(worktree),
        "passed": agent_exit_code == 0 and all(v["exit_code"] == 0 for v in verifier_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", default="evals/model-routing/spark_fast_code.jsonl")
    parser.add_argument("--model", action="append", default=[], help="Repeatable provider:model route")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=900, help="Timeout seconds per live Hermes call and verifier")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print planned runs without invoking models")
    parser.add_argument("--live", action="store_true", help="Actually run Hermes in isolated git worktrees")
    parser.add_argument("--cleanup", action="store_true", help="Remove worktrees after live runs")
    parser.add_argument("--out", default="tmp/model-routing-eval")
    args = parser.parse_args()

    if args.live and args.dry_run:
        raise SystemExit("Choose either --live or --dry-run, not both")
    if not args.live:
        args.dry_run = True

    root = repo_root()
    pack = Path(args.pack)
    if not pack.is_absolute():
        pack = root / pack
    cases = load_pack(pack)
    if args.limit:
        cases = cases[: args.limit]
    models = [parse_model(m) for m in (args.model or DEFAULT_MODELS)]

    planned = [(c, provider, model) for c in cases for provider, model in models]
    if args.dry_run:
        print(f"Dry run: loaded {len(cases)} cases from {pack}")
        print(f"Planned runs: {len(planned)}")
        for case, provider, model in planned:
            print(f"- {case['id']} :: {provider}:{model} :: verifiers={len(case['verifiers'])}")
        return 0

    out_dir = root / args.out / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    summary = defaultdict(lambda: {"runs": 0, "passed": 0, "seconds": 0.0})
    worktrees: list[Path] = []
    with results_path.open("w") as fh:
        for case, provider, model in planned:
            row = live_case(root, out_dir, case, provider, model, args.timeout)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            key = f"{provider}:{model}"
            summary[key]["runs"] += 1
            summary[key]["passed"] += int(bool(row["passed"]))
            summary[key]["seconds"] += float(row["duration_seconds"])
            worktrees.append(Path(row["worktree"]))
            print(f"{row['case_id']} {key} passed={row['passed']} duration={row['duration_seconds']}s")

    print(f"\nResults: {results_path}")
    print("\nSummary by model")
    print("| model | passed | runs | avg seconds |")
    print("|---|---:|---:|---:|")
    for key, data in sorted(summary.items()):
        avg = data["seconds"] / max(1, data["runs"])
        print(f"| {key} | {data['passed']} | {data['runs']} | {avg:.1f} |")

    if args.cleanup:
        for wt in worktrees:
            remove_worktree(root, wt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
