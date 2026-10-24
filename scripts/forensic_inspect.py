#!/usr/bin/env python3
"""Forensic Trajectory Inspector & Baseline Recovery Auditor for Gemma 4 E4B (Track 2).

Provides 5 diagnostic modes:
  1. --compare-prompts      : Audits src/prompts/system.md, src/configs/sampling.yaml, and
                              src/agent.yaml against all 6 Phase 1 E4B failure modes.
  2. --select-sample-ids    : Streams tasks.jsonl (without dumping full task bodies) to select
                              4 representative instance_ids (1 per repo: fastapi, rich,
                              requests, httpx) and prints the exact swegemma eval command.
  3. --results-dir <dir>    : Parses swegemma eval output artifacts (traces/trace_*.json,
                              logs/*.log, patches/*.patch, task_results.jsonl) and reports:
                              - Structured tool calls vs. conversational markdown blocks
                              - Continuation nudges and MAX_TOKENS / <|tool_call> truncations
                              - Exact arguments passed to run_command and edit_file
                              - Literal placeholder leaks ('PKG/', SymbolOrError, TESTFILE)
                              - FileEditError and CommandError (pathspec) counts
                              - submit_patch invocation, patch_size, and Phase 2 resolution
  4. --live-endpoint-probe  : Probes a live vLLM endpoint (/v1/chat/completions) on the 4
                              representative tasks from tasks.jsonl.
  5. --self-test            : End-to-end verification of the trajectory forensic parser on
                              synthetic Phase 1 failed vs. recovered traces.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FORBIDDEN_PLACEHOLDERS = (
    "'PKG/'",
    '"PKG/"',
    "/workspace/PKG/",
    "PKG/module.py",
    "SymbolOrError",
    "TESTFILE",
    "-k KEYWORD",
    "py_compile FILE",
)

MARKDOWN_CODE_BLOCK_RE = re.compile(r"```(?:bash|sh|python|py|diff)?\s*\n", flags=re.IGNORECASE)
NUDGE_PATTERNS = (
    "Please continue your work using the available tools",
    "Your previous response reached the token limit",
    "<|tool_call|> was cut off",
)


def ensure_lf_line_endings(filepath: Path) -> None:
    """Normalizes CRLF to LF in-place to save prompt bytes and prevent bash heredoc CR issues."""
    if not filepath.is_file():
        return
    raw = filepath.read_bytes()
    if b"\r\n" in raw:
        filepath.write_bytes(raw.replace(b"\r\n", b"\n"))


def audit_configuration(repo_root: Path) -> bool:
    """Audits src/prompts/system.md, src/configs/sampling.yaml, and src/agent.yaml against the 6 failure modes."""
    src_dir = repo_root / "src"
    system_md = src_dir / "prompts" / "system.md"
    sampling_yaml = src_dir / "configs" / "sampling.yaml"
    agent_yaml = src_dir / "agent.yaml"

    for p in (system_md, sampling_yaml, agent_yaml):
        ensure_lf_line_endings(p)

    prompt_bytes = system_md.read_bytes()
    prompt_text = prompt_bytes.decode("utf-8")
    sampling_text = sampling_yaml.read_text(encoding="utf-8")
    agent_text = agent_yaml.read_text(encoding="utf-8")

    print("=== Gemma 4 E4B Forensic Configuration Audit (6 Failure Modes) ===")
    checks: List[Tuple[str, bool, str]] = []

    # Invariant 0: Zero curly braces & byte ceiling
    has_braces = ("{" in prompt_text) or ("}" in prompt_text)
    checks.append(
        (
            "ADK Session Template Safety (0 curly braces)",
            not has_braces,
            f"braces_found={has_braces}, size={len(prompt_bytes):,}/4,096 bytes",
        )
    )
    checks.append(
        (
            "Small-Model Prompt Ceiling (<= 4,096 bytes, LF endings)",
            len(prompt_bytes) <= 4096 and b"\r\n" not in prompt_bytes,
            f"{len(prompt_bytes):,} bytes (saved {4096 - len(prompt_bytes):,} bytes below ceiling)",
        )
    )

    # FM1: Literal 'PKG/' and abstract placeholders
    leaked_placeholders = [ph for ph in FORBIDDEN_PLACEHOLDERS if ph in prompt_text]
    checks.append(
        (
            "FM1 Fix: Zero Abstract Pathspec Placeholders ('PKG/', SymbolOrError)",
            len(leaked_placeholders) == 0,
            "Clean (0 placeholders)" if not leaked_placeholders else f"Found: {leaked_placeholders}",
        )
    )

    # FM2: Anti-markdown code block rule & structured tool-call few-shot examples
    has_raw_heredoc = "<< 'PYEOF'" in prompt_text or '<< "PYEOF"' in prompt_text
    has_tool_examples = (
        "run_command(command=" in prompt_text
        and "read_file(filepath=" in prompt_text
        and "edit_file(" in prompt_text
    )
    has_no_md_rule = "NEVER output markdown code blocks" in prompt_text
    checks.append(
        (
            "FM2 Fix: Structured Tool-Call Few-Shots & Anti-Markdown Rule",
            (not has_raw_heredoc) and has_tool_examples and has_no_md_rule,
            f"tool_examples={has_tool_examples}, anti_markdown_rule={has_no_md_rule}, raw_heredoc={has_raw_heredoc}",
        )
    )

    # FM3: Thinking budget >= 1024 and include_thoughts: false
    m_budget = re.search(r"thinking_budget:\s*(\d+)", sampling_text)
    m_inc = re.search(r"include_thoughts:\s*(true|false)", sampling_text, flags=re.IGNORECASE)
    budget_val = int(m_budget.group(1)) if m_budget else 0
    inc_val = (m_inc.group(1).lower() == "true") if m_inc else True
    checks.append(
        (
            "FM3 Fix: Anti-Truncation Budget (>=1024) & History Stripping (include_thoughts: false)",
            budget_val >= 1024 and (inc_val is False),
            f"thinking_budget={budget_val}, include_thoughts={inc_val}",
        )
    )

    # FM4: Deterministic edit temperature (0.0 <= temp <= 0.3)
    m_temp = re.search(r"temperature:\s*([0-9.]+)", sampling_text)
    temp_val = float(m_temp.group(1)) if m_temp else 1.0
    checks.append(
        (
            "FM4 Fix: Low Temperature for Exact old_string Copying (<= 0.3)",
            0.0 <= temp_val <= 0.3,
            f"temperature={temp_val}",
        )
    )

    # FM5: Linear 4-step workflow without Method B line-offset scripts
    has_method_b = "/tmp/edit.py" in prompt_text or "splitlines(True)" in prompt_text
    checks.append(
        (
            "FM5 Fix: Linear 4-Step Loop (Removed Method B Line-Counting Script)",
            not has_method_b,
            f"method_b_present={has_method_b}",
        )
    )

    # FM6: Pre-computed symbol graph tools attached in agent.yaml
    has_graph_tools = (
        "search_similar_code" in agent_text and "get_code_neighbors" in agent_text
    )
    checks.append(
        (
            "FM6 Fix: Code Intelligence Tools Attached (search_similar_code, get_code_neighbors)",
            has_graph_tools,
            f"graph_tools_attached={has_graph_tools}",
        )
    )

    all_ok = True
    for title, passed, detail in checks:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"  {status} {title}: {detail}")
        if not passed:
            all_ok = False

    print()
    return all_ok


def select_representative_tasks(tasks_jsonl: Path, per_repo: int = 1) -> List[Dict[str, Any]]:
    """Streams tasks.jsonl line-by-line and picks `per_repo` tasks per repository without dumping full file."""
    if not tasks_jsonl.is_file():
        raise FileNotFoundError(f"tasks.jsonl not found at: {tasks_jsonl}")

    target_repos = ["fastapi", "rich", "requests", "httpx"]
    selected_by_repo: Dict[str, List[Dict[str, Any]]] = {r: [] for r in target_repos}

    with open(tasks_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            instance_id = str(row.get("instance_id") or row.get("id") or "")
            repo = str(row.get("repo") or "")
            problem = str(row.get("problem_statement") or "").strip()
            hints = str(row.get("hints_text") or "").strip()

            matched_repo: Optional[str] = None
            for r in target_repos:
                if r in repo.lower() or instance_id.lower().startswith(r):
                    matched_repo = r
                    break

            if matched_repo and len(selected_by_repo[matched_repo]) < per_repo:
                first_line = problem.splitlines()[0][:85] if problem else ""
                safe_summary = first_line.encode("ascii", errors="ignore").decode("ascii").strip()
                selected_by_repo[matched_repo].append(
                    {
                        "instance_id": instance_id,
                        "repo": repo,
                        "has_hints": bool(hints),
                        "summary": safe_summary,
                        "problem_statement": problem,
                        "hints_text": hints,
                    }
                )
            if all(len(v) >= per_repo for v in selected_by_repo.values()):
                break

    flat: List[Dict[str, Any]] = []
    for r in target_repos:
        flat.extend(selected_by_repo[r])
    return flat


def print_sample_selection(tasks_jsonl: Path, repo_root: Path) -> None:
    """Prints 4 representative benchmark task IDs and the exact local harness CLI commands."""
    samples = select_representative_tasks(tasks_jsonl, per_repo=1)
    harness_dir = tasks_jsonl.parent
    print("=== Selected 4 Representative Benchmark Tasks (1 per Repo) ===")
    ids: List[str] = []
    for s in samples:
        ids.append(s["instance_id"])
        print(
            f"  - {s['instance_id']:<20} | repo={s['repo']:<18} | hints={str(s['has_hints']):<5} | {s['summary']}..."
        )

    id_str = " ".join(ids)
    results_out = repo_root / "results" / "e4b_recovery_check"
    print("\n=== Command 1: Run Local Harness on the 4 Sample Tasks ===")
    print(
        f"swegemma eval \\\n"
        f"  --tasks \"{tasks_jsonl}\" \\\n"
        f"  --snapshots-dir \"{harness_dir / 'snapshots'}\" \\\n"
        f"  --submission-dir \"{repo_root / 'src'}\" \\\n"
        f"  --results-dir \"{results_out}\" \\\n"
        f"  --task-ids {id_str} \\\n"
        f"  --sandbox docker \\\n"
        f"  --max-tool-calls 50 \\\n"
        f"  --max-time-minutes 6 \\\n"
        f"  --timeout-seconds 75 \\\n"
        f"  --concurrency 1"
    )
    print("\n=== Command 2: Inspect Raw Trajectories & Autopsy Metrics ===")
    print(f"python scripts/forensic_inspect.py --results-dir \"{results_out}\"")


def inspect_single_trace(
    instance_id: str,
    trace_data: Dict[str, Any],
    log_text: str,
    patch_text: str,
    task_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Extracts forensic failure-mode telemetry from a single task's trace, log, and patch."""
    steps = (
        trace_data.get("steps")
        or trace_data.get("events")
        or trace_data.get("messages")
        or trace_data.get("turns")
        or []
    )

    structured_tool_calls = 0
    markdown_block_turns = 0
    text_only_turns = 0
    unclosed_tool_tags = 0
    max_tokens_cuts = 0
    placeholder_leaks: List[str] = []
    run_commands: List[str] = []
    edit_calls: List[Dict[str, Any]] = []
    search_calls: List[str] = []
    file_edit_errors = 0
    pathspec_errors = 0
    command_errors = 0
    submit_patch_called = False

    for step in steps:
        if not isinstance(step, dict):
            continue
        role = str(step.get("role") or step.get("author") or step.get("source") or "").lower()
        content = str(step.get("content") or step.get("text") or step.get("message") or "")
        finish_reason = str(step.get("finish_reason") or "").lower()

        tcs = step.get("tool_calls") or step.get("function_calls") or []
        if isinstance(step.get("action"), dict) and step["action"].get("tool_name"):
            tcs = [
                {
                    "name": step["action"]["tool_name"],
                    "arguments": step["action"].get("arguments", {}),
                }
            ]

        if role in {"assistant", "model", "agent", "lead_agent"} or tcs:
            if "<|tool_call>" in content and "<tool_call|>" not in content:
                unclosed_tool_tags += 1
            if "length" in finish_reason or "max_tokens" in finish_reason:
                max_tokens_cuts += 1

            if tcs:
                structured_tool_calls += len(tcs)
                for tc in tcs:
                    fn = tc.get("function", tc) if isinstance(tc, dict) else {}
                    t_name = str(fn.get("name") or tc.get("name") or "")
                    raw_args = fn.get("arguments") if "arguments" in fn else tc.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except Exception:
                            args = {"raw": raw_args}
                    elif isinstance(raw_args, dict):
                        args = raw_args
                    else:
                        args = {}

                    raw_dump = json.dumps(args)
                    for ph in FORBIDDEN_PLACEHOLDERS:
                        if ph in raw_dump and ph not in placeholder_leaks:
                            placeholder_leaks.append(ph)

                    if t_name == "run_command":
                        cmd = str(args.get("command", ""))
                        run_commands.append(cmd)
                    elif t_name == "edit_file":
                        edit_calls.append(
                            {
                                "filepath": str(args.get("filepath", "")),
                                "old_len": len(str(args.get("old_string", "")).splitlines()),
                                "new_len": len(str(args.get("new_string", "")).splitlines()),
                            }
                        )
                    elif t_name in {"search_similar_code", "get_code_neighbors"}:
                        search_calls.append(f"{t_name}({args.get('query') or args.get('node')})")
                    elif t_name == "submit_patch":
                        submit_patch_called = True
            else:
                text_only_turns += 1
                if MARKDOWN_CODE_BLOCK_RE.search(content):
                    markdown_block_turns += 1

        # Inspect tool responses / observations for errors
        tool_resp_str = json.dumps(step)
        if '"FileEditError"' in tool_resp_str or "FileEditError" in content:
            file_edit_errors += 1
        if "did not match any files" in tool_resp_str or "pathspec" in tool_resp_str:
            pathspec_errors += 1
        if '"CommandError"' in tool_resp_str:
            command_errors += 1

    # Also scan human-readable log_text for nudges and fallback indicators
    nudge_count = sum(log_text.count(pat) for pat in NUDGE_PATTERNS)
    if "submit_patch" in log_text and not submit_patch_called:
        if '"patch_size"' in log_text:
            submit_patch_called = True

    patch_bytes = len(patch_text.encode("utf-8")) if patch_text.strip() else int(task_result.get("patch_size", 0) or 0)
    resolved = bool(task_result.get("resolved", False))

    return {
        "instance_id": instance_id,
        "resolved": resolved,
        "structured_tool_calls": structured_tool_calls,
        "text_only_turns": text_only_turns,
        "markdown_block_turns": markdown_block_turns,
        "nudge_count": nudge_count,
        "unclosed_tool_tags": unclosed_tool_tags,
        "max_tokens_cuts": max_tokens_cuts,
        "placeholder_leaks": placeholder_leaks,
        "run_commands": run_commands,
        "edit_calls": edit_calls,
        "search_calls": search_calls,
        "file_edit_errors": file_edit_errors,
        "pathspec_errors": pathspec_errors,
        "command_errors": command_errors,
        "submit_patch_called": submit_patch_called,
        "patch_bytes": patch_bytes,
    }


def analyze_results_directory(results_dir: Path) -> Dict[str, Any]:
    """Parses a swegemma eval results directory and prints a forensic report."""
    if not results_dir.is_dir():
        raise FileNotFoundError(f"Results directory does not exist: {results_dir}")

    task_results_map: Dict[str, Dict[str, Any]] = {}
    tr_file = results_dir / "task_results.jsonl"
    if tr_file.is_file():
        for line in tr_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                tid = str(row.get("instance_id") or row.get("task_id") or row.get("id") or "")
                if tid:
                    task_results_map[tid] = row

    traces_dir = results_dir / "traces"
    logs_dir = results_dir / "logs"
    patches_dir = results_dir / "patches"

    instance_ids = set(task_results_map.keys())
    if traces_dir.is_dir():
        for p in traces_dir.glob("trace_*.json"):
            instance_ids.add(p.stem.replace("trace_", "", 1))
    if patches_dir.is_dir():
        for p in patches_dir.glob("*.patch"):
            instance_ids.add(p.stem)

    reports: List[Dict[str, Any]] = []
    for iid in sorted(instance_ids):
        trace_path = traces_dir / f"trace_{iid}.json"
        log_path = logs_dir / f"{iid}.log"
        patch_path = patches_dir / f"{iid}.patch"

        trace_data = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.is_file() else {}
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.is_file() else ""
        t_res = task_results_map.get(iid, {})

        rep = inspect_single_trace(iid, trace_data, log_text, patch_text, t_res)
        reports.append(rep)

    print(f"\n=== Forensic Trajectory Autopsy Report: {results_dir.name} ({len(reports)} Tasks) ===")
    for r in reports:
        status = "RESOLVED" if r["resolved"] else ("PATCHED" if r["patch_bytes"] > 0 else "EMPTY_DIFF")
        print(
            f"\n[{status}] {r['instance_id']} | patch={r['patch_bytes']}B | submit_patch={r['submit_patch_called']}"
        )
        print(
            f"  - Tool Calls : {r['structured_tool_calls']} structured | "
            f"markdown_blocks={r['markdown_block_turns']} | nudges={r['nudge_count']} | "
            f"truncations={r['unclosed_tool_tags'] + r['max_tokens_cuts']}"
        )
        print(
            f"  - Tool Errors: FileEditError={r['file_edit_errors']} | "
            f"pathspec_err={r['pathspec_errors']} | CommandError={r['command_errors']} | "
            f"placeholder_leaks={r['placeholder_leaks']}"
        )
        if r["search_calls"]:
            print(f"  - Graph Calls: {r['search_calls'][:3]}")
        if r["run_commands"]:
            print(f"  - Commands   : {r['run_commands'][:3]}")
        if r["edit_calls"]:
            print(f"  - Edits      : {r['edit_calls']}")

    total = len(reports)
    non_empty = sum(1 for r in reports if r["patch_bytes"] > 0)
    resolved_cnt = sum(1 for r in reports if r["resolved"])
    md_leaks = sum(r["markdown_block_turns"] for r in reports)
    ph_leaks = sum(len(r["placeholder_leaks"]) for r in reports)

    summary = {
        "total_tasks": total,
        "non_empty_patches": non_empty,
        "resolved_tasks": resolved_cnt,
        "markdown_block_turns": md_leaks,
        "placeholder_leaks": ph_leaks,
    }
    print(
        f"\n=== Aggregate Summary: {non_empty}/{total} non-empty patches | "
        f"{resolved_cnt}/{total} resolved | markdown_leaks={md_leaks} | placeholder_leaks={ph_leaks} ==="
    )
    return summary


def run_live_endpoint_probe(
    api_base: str,
    model_name: str,
    tasks_jsonl: Path,
    repo_root: Path,
) -> bool:
    """Probes a live vLLM endpoint on the 4 representative tasks from tasks.jsonl."""
    sys.path.insert(0, str(repo_root / "scripts" / "sft"))
    from build_gemma4_sft import OFFICIAL_TOOLS
    from curate_trajectories import build_harness_initial_user_prompt

    system_md = (repo_root / "src" / "prompts" / "system.md").read_text(encoding="utf-8")
    samples = select_representative_tasks(tasks_jsonl, per_repo=1)

    passed = 0
    for s in samples:
        user_msg = build_harness_initial_user_prompt(
            s["repo"], s["problem_statement"], s["hints_text"]
        )
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_md},
                {"role": "user", "content": user_msg},
            ],
            "tools": OFFICIAL_TOOLS,
            "tool_choice": "auto",
            "temperature": 0.2,
            "top_p": 0.95,
            "max_tokens": 4096,
            "seed": 42,
            "extra_body": {
                "top_k": 64,
                "chat_template_kwargs": {"enable_thinking": True},
                "thinking_token_budget": 1536,
            },
        }
        req = urllib.request.Request(
            f"{api_base.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"[FAIL] {s['instance_id']}: HTTP request failed: {exc}")
            return False

        msg = body["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        content = msg.get("content") or ""
        if not tcs or MARKDOWN_CODE_BLOCK_RE.search(content):
            print(f"[FAIL] {s['instance_id']}: No structured tool call or emitted markdown block!")
            return False

        fn = tcs[0]["function"]
        args_str = json.dumps(fn.get("arguments", ""))
        leaked = [ph for ph in FORBIDDEN_PLACEHOLDERS if ph in args_str]
        if leaked:
            print(f"[FAIL] {s['instance_id']}: Placeholder leaked in tool args: {leaked}")
            return False

        print(f"[OK] {s['instance_id']} -> {fn['name']}({fn.get('arguments')})")
        passed += 1

    print(f"\n=== Live 4-Task Probe Passed: {passed}/{len(samples)} ===")
    return passed == len(samples)


def run_self_test() -> bool:
    """Verifies inspect_single_trace and analyze_results_directory on synthetic failed vs. recovered runs."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 1. Simulate Phase 1 Failed Run (0.0 score)
        fail_dir = tmp_path / "phase1_failed"
        (fail_dir / "traces").mkdir(parents=True)
        (fail_dir / "logs").mkdir(parents=True)
        (fail_dir / "patches").mkdir(parents=True)

        failed_trace = {
            "steps": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "run_command",
                                "arguments": {"command": "git grep -n -E \"SymbolOrError\" -- 'PKG/' | head -30"},
                            }
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": '{"status": "error", "error_type": "CommandError", "error_message": "fatal: pathspec \'PKG/\' did not match any files"}',
                },
                {
                    "role": "assistant",
                    "content": "```bash\ncat > /tmp/repro.py << 'PYEOF'\nprint('hi')\nPYEOF\n```",
                    "tool_calls": [],
                },
            ]
        }
        (fail_dir / "traces" / "trace_fastapi__11194.json").write_text(
            json.dumps(failed_trace), encoding="utf-8"
        )
        (fail_dir / "logs" / "fastapi__11194.log").write_text(
            "Please continue your work using the available tools", encoding="utf-8"
        )
        (fail_dir / "patches" / "fastapi__11194.patch").write_text("", encoding="utf-8")
        (fail_dir / "task_results.jsonl").write_text(
            json.dumps({"instance_id": "fastapi__11194", "resolved": False, "patch_size": 0}) + "\n",
            encoding="utf-8",
        )

        fail_summary = analyze_results_directory(fail_dir)
        assert fail_summary["non_empty_patches"] == 0
        assert fail_summary["markdown_block_turns"] == 1
        assert fail_summary["placeholder_leaks"] >= 1

        # 2. Simulate Phase 1 Recovered Run (non-empty patch + resolved)
        rec_dir = tmp_path / "phase1_recovered"
        (rec_dir / "traces").mkdir(parents=True)
        (rec_dir / "logs").mkdir(parents=True)
        (rec_dir / "patches").mkdir(parents=True)

        rec_trace = {
            "steps": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "search_similar_code",
                                "arguments": {"query": "serialize_response"},
                            }
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": {"filepath": "fastapi/routing.py", "start_line": 120, "end_line": 180},
                            }
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "edit_file",
                                "arguments": {
                                    "filepath": "fastapi/routing.py",
                                    "old_string": "    if field:\n        return value",
                                    "new_string": "    if field is not None:\n        return value",
                                },
                            }
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "submit_patch", "arguments": {}}}],
                },
            ]
        }
        (rec_dir / "traces" / "trace_fastapi__11194.json").write_text(
            json.dumps(rec_trace), encoding="utf-8"
        )
        (rec_dir / "logs" / "fastapi__11194.log").write_text("status: ok", encoding="utf-8")
        (rec_dir / "patches" / "fastapi__11194.patch").write_text(
            "--- a/fastapi/routing.py\n+++ b/fastapi/routing.py\n@@ -120,2 +120,2 @@\n-    if field:\n+    if field is not None:\n",
            encoding="utf-8",
        )
        (rec_dir / "task_results.jsonl").write_text(
            json.dumps({"instance_id": "fastapi__11194", "resolved": True, "patch_size": 112}) + "\n",
            encoding="utf-8",
        )

        rec_summary = analyze_results_directory(rec_dir)
        assert rec_summary["non_empty_patches"] == 1
        assert rec_summary["resolved_tasks"] == 1
        assert rec_summary["markdown_block_turns"] == 0
        assert rec_summary["placeholder_leaks"] == 0

    print("\n[OK] Forensic Trajectory Inspector Self-Test Passed Cleanly!")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forensic Trajectory Inspector & Config Auditor for Gemma 4 E4B."
    )
    repo_root = Path(__file__).resolve().parent.parent
    default_tasks = repo_root.parent / "gemma-4-developer-agent" / "tasks.jsonl"

    parser.add_argument(
        "--compare-prompts",
        action="store_true",
        help="Audit src/prompts/system.md, sampling.yaml, and agent.yaml against the 6 failure modes.",
    )
    parser.add_argument(
        "--select-sample-ids",
        action="store_true",
        help="Select 4 representative benchmark tasks (1 per repo) from tasks.jsonl.",
    )
    parser.add_argument(
        "--tasks-jsonl",
        type=Path,
        default=default_tasks,
        help="Path to benchmark tasks.jsonl.",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="Path to swegemma eval results directory to inspect.",
    )
    parser.add_argument(
        "--live-endpoint-probe",
        action="store_true",
        help="Probe a live vLLM endpoint on the 4 sample tasks.",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default="http://127.0.0.1:8000/v1",
        help="OpenAI-compatible vLLM endpoint URL.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gemma-4-e4b-it-qat-w4a16-ct",
        help="Served model alias.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run automated self-test on synthetic failed vs. recovered trace artifacts.",
    )
    args = parser.parse_args()

    ran_any = False
    ok = True

    if args.compare_prompts:
        ran_any = True
        ok = audit_configuration(repo_root) and ok

    if args.select_sample_ids:
        ran_any = True
        print_sample_selection(args.tasks_jsonl, repo_root)

    if args.results_dir:
        ran_any = True
        analyze_results_directory(args.results_dir)

    if args.live_endpoint_probe:
        ran_any = True
        ok = run_live_endpoint_probe(args.api_base, args.model, args.tasks_jsonl, repo_root) and ok

    if args.self_test:
        ran_any = True
        ok = run_self_test() and ok

    if not ran_any:
        ok = audit_configuration(repo_root)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
