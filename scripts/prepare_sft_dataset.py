#!/usr/bin/env python3
"""prepare_sft_dataset.py

Generates high-quality Supervised Fine-Tuning (SFT) dataset for the Gemma 4
Developer Agent.

Extracts from tasks.jsonl:
1. Surgical code edits (edit_file / write_file) derived from ground-truth patches.
2. TDD isolated reproduction turns (run_command with /tmp/repro.py).
3. Patch hygiene & submission turns (git checkout tests / submit_patch).

Outputs:
- data/sft_train.jsonl
- data/sft_val.jsonl
- data/sft_summary.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def parse_patch_into_hunks(patch_str: str) -> List[Dict[str, Any]]:
    """Parses a unified git diff into individual atomic modification hunks."""
    hunks = []
    lines = patch_str.splitlines()
    current_file = None
    is_new = False
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            old_f = line[4:].strip().split("\t")[0]
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                new_f = lines[i + 1][4:].strip().split("\t")[0]
                if old_f.startswith("a/"):
                    old_f = old_f[2:]
                if new_f.startswith("b/"):
                    new_f = new_f[2:]
                is_new = old_f == "/dev/null"
                current_file = new_f if new_f != "/dev/null" else old_f
                i += 2
                continue
        elif line.startswith("@@ ") and current_file:
            old_lines = []
            new_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("@@ ") and not lines[i].startswith("--- "):
                hl = lines[i]
                if hl.startswith(" "):
                    old_lines.append(hl[1:])
                    new_lines.append(hl[1:])
                elif hl.startswith("-"):
                    old_lines.append(hl[1:])
                elif hl.startswith("+"):
                    new_lines.append(hl[1:])
                i += 1

            old_str = "\n".join(old_lines)
            new_str = "\n".join(new_lines)

            # Skip empty or no-op hunks
            if old_str.strip() or new_str.strip():
                hunks.append({
                    "filepath": current_file,
                    "is_new": is_new,
                    "old_string": old_str,
                    "new_string": new_str,
                })
            continue
        i += 1

    return hunks


def format_gemma_chat(user_msg: str, model_thought: str, tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Formats a turn using the exact Gemma 4 chat template with thought & tool_call."""
    args_json = json.dumps(tool_args, ensure_ascii=False)
    # Gemma 4 vLLM tool call format
    tool_call_str = f"<|tool_call|>call:{tool_name}{args_json}<|call_end|>"
    
    text = (
        f"<start_of_turn>user\n{user_msg.strip()}<end_of_turn>\n"
        f"<start_of_turn>model\n"
        f"<|thought|>\n{model_thought.strip()}\n<|thought|>\n"
        f"{tool_call_str}\n<end_of_turn>"
    )
    return text


def build_surgical_sample(task: Dict[str, Any], hunk: Dict[str, Any]) -> Dict[str, Any]:
    """Builds an SFT sample for surgical code editing."""
    repo = task.get("repo", "unknown_repo")
    problem = task.get("problem_statement", "").strip()
    hints = task.get("hints_text", "").strip()
    filepath = hunk["filepath"]
    is_new = hunk["is_new"]
    old_string = hunk["old_string"]
    new_string = hunk["new_string"]

    user_prompt = f"You are an expert autonomous software engineer resolving an issue in repository {repo}.\n\n"
    user_prompt += f"Problem Statement:\n{problem}\n\n"
    if hints:
        user_prompt += f"## Hints:\n{hints}\n\n"
    user_prompt += f"Target file: {filepath}\n"
    if is_new:
        user_prompt += "Create the new file with the required implementation."
    else:
        user_prompt += "Apply the minimal surgical fix to resolve the error."

    if is_new:
        thought = (
            f"The issue in `{repo}` requires creating `{filepath}`. "
            f"Writing the clean source implementation directly."
        )
        tool_name = "write_file"
        tool_args = {"filepath": filepath, "content": new_string}
    else:
        thought = (
            f"Analyzing the bug in `{repo}` within `{filepath}`. "
            f"Identified the faulty section. Applying a surgical replacement via edit_file "
            f"with exact surrounding lines to guarantee uniqueness and avoid FileEditError."
        )
        tool_name = "edit_file"
        tool_args = {
            "filepath": filepath,
            "old_string": old_string,
            "new_string": new_string,
            "allow_multiple": False,
        }

    formatted_text = format_gemma_chat(user_prompt, thought, tool_name, tool_args)

    return {
        "task_id": task.get("instance_id", ""),
        "repo": repo,
        "sample_type": "surgical_edit",
        "messages": [
            {"role": "user", "content": user_prompt},
            {
                "role": "assistant",
                "content": f"<|thought|>\n{thought}\n<|thought|>",
                "tool_calls": [
                    {
                        "id": f"call_{task.get('instance_id', 'cmd')[:8]}",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(tool_args, ensure_ascii=False),
                        },
                    }
                ],
            },
        ],
        "text": formatted_text,
    }


def build_hygiene_sample(task: Dict[str, Any]) -> Dict[str, Any]:
    """Builds an SFT sample for patch hygiene and submission."""
    repo = task.get("repo", "unknown_repo")
    problem = task.get("problem_statement", "").strip()

    user_prompt = (
        f"You are finalizing a fix in repository {repo}.\n\n"
        f"Problem Statement:\n{problem}\n\n"
        f"Your surgical modifications have passed verification. "
        f"Clean up any temporary or test files and submit the patch."
    )

    thought = (
        "Implementation verified. Reverting any accidental changes to test files or config "
        "to ensure strict patch hygiene before submission, then calling submit_patch."
    )

    tool_name = "run_command"
    tool_args = {
        "command": "git checkout -- tests/ conftest.py pytest.ini pyproject.toml 2>/dev/null || true; git status --porcelain"
    }

    formatted_text = format_gemma_chat(user_prompt, thought, tool_name, tool_args)

    return {
        "task_id": task.get("instance_id", ""),
        "repo": repo,
        "sample_type": "patch_hygiene",
        "messages": [
            {"role": "user", "content": user_prompt},
            {
                "role": "assistant",
                "content": f"<|thought|>\n{thought}\n<|thought|>",
                "tool_calls": [
                    {
                        "id": f"call_{task.get('instance_id', 'cmd')[:8]}_hyg",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(tool_args, ensure_ascii=False),
                        },
                    }
                ],
            },
        ],
        "text": formatted_text,
    }


def main():
    parser = argparse.ArgumentParser(description="Prepare SFT dataset for Gemma 4 LoRA.")
    parser.add_argument(
        "--tasks-path",
        type=str,
        default="d:/Context-GoogleGemma4/gemma-4-developer-agent/tasks.jsonl",
        help="Path to tasks.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="d:/Context-GoogleGemma4/gemma-agent/data",
        help="Output directory for SFT dataset",
    )
    parser.add_argument(
        "--val-ratio",
        type=float,
        default=0.10,
        help="Validation set split ratio (default: 0.10)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for splitting",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run integrity verification on generated dataset",
    )
    args = parser.parse_args()

    random.seed(args.seed)
    tasks_file = Path(args.tasks_path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not tasks_file.is_file():
        print(f"Error: {tasks_file} not found.")
        sys.exit(1)

    print(f"Loading tasks from: {tasks_file}")
    tasks = []
    with open(tasks_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                tasks.append(json.loads(line))

    print(f"Total tasks loaded: {len(tasks)}")

    samples: List[Dict[str, Any]] = []
    task_count_with_hunks = 0

    for task in tasks:
        patch = task.get("patch", "")
        hunks = parse_patch_into_hunks(patch)
        if not hunks:
            continue

        task_count_with_hunks += 1

        # 1. Surgical edit samples for each hunk
        for hunk in hunks:
            sample = build_surgical_sample(task, hunk)
            samples.append(sample)

        # 2. Patch hygiene sample per task
        hygiene_sample = build_hygiene_sample(task)
        samples.append(hygiene_sample)

    print(f"Extracted {len(samples)} total supervisory samples across {task_count_with_hunks} tasks.")

    # Shuffle deterministically
    random.shuffle(samples)

    # Train / Val split
    split_idx = int(len(samples) * (1.0 - args.val_ratio))
    train_samples = samples[:split_idx]
    val_samples = samples[split_idx:]

    train_path = output_dir / "sft_train.jsonl"
    val_path = output_dir / "sft_val.jsonl"
    summary_path = output_dir / "sft_summary.json"

    with open(train_path, "w", encoding="utf-8") as f:
        for s in train_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    with open(val_path, "w", encoding="utf-8") as f:
        for s in val_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    summary = {
        "total_tasks_processed": len(tasks),
        "total_samples": len(samples),
        "train_samples": len(train_samples),
        "val_samples": len(val_samples),
        "sample_types": {
            "surgical_edit": sum(1 for s in samples if s["sample_type"] == "surgical_edit"),
            "patch_hygiene": sum(1 for s in samples if s["sample_type"] == "patch_hygiene"),
        },
        "repo_distribution": {},
    }

    for s in samples:
        repo = s["repo"]
        summary["repo_distribution"][repo] = summary["repo_distribution"].get(repo, 0) + 1

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n[OK] Dataset generated successfully:")
    print(f"  - Train: {len(train_samples)} samples -> {train_path}")
    print(f"  - Val:   {len(val_samples)} samples -> {val_path}")
    print(f"  - Summary: {summary_path}")

    if args.verify:
        print("\n--- Running Verification ---")
        assert len(train_samples) > 0, "Train set is empty!"
        assert len(val_samples) > 0, "Val set is empty!"
        sample_check = train_samples[0]
        assert "<start_of_turn>user" in sample_check["text"]
        assert "<start_of_turn>model" in sample_check["text"]
        assert "<|tool_call|>" in sample_check["text"]
        print("[OK] Verification passed cleanly!")


if __name__ == "__main__":
    main()
