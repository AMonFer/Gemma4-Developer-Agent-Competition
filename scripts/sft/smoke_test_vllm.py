#!/usr/bin/env python3
"""Component D.2: Pre-Submission vLLM Gemma 4 Parser & LoRA Smoke Test.

Tests 20 held-out SWE prompts against a live vLLM endpoint running:
  vllm serve google/gemma-4-e4b-it-qat-w4a16-ct \
    --enable-lora \
    --lora-modules main_lora=./src/adapters/main_lora \
    --max-lora-rank 32 \
    --enable-auto-tool-choice \
    --tool-call-parser gemma4 \
    --reasoning-parser gemma4 \
    --max-model-len 32768

Also supports `--offline-self-test` to verify the vLLM `gemma4` tool-call and reasoning
parser regexes against generated SFT completions before launching the GPU server.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_gemma4_sft import OFFICIAL_TOOLS  # noqa: E402

VALID_TOOL_NAMES = {t["function"]["name"] for t in OFFICIAL_TOOLS}
REQUIRED_PARAMS = {
    t["function"]["name"]: set(t["function"]["parameters"].get("required", []))
    for t in OFFICIAL_TOOLS
}

# Exact regex matching vLLM's built-in Gemma4ToolParser (`<|tool_call>call:name{...}<tool_call|>`)
GEMMA4_TOOL_CALL_REGEX = re.compile(
    r"<\|tool_call>call:([A-Za-z_][A-Za-z0-9_]*)\{(.*?)\}<tool_call\|>",
    flags=re.DOTALL,
)
GEMMA4_THOUGHT_REGEX = re.compile(
    r"<\|channel>thought\n(.*?)<channel\|>",
    flags=re.DOTALL,
)


def parse_gemma4_tool_arguments(raw_arg_body: str) -> Dict[str, Any]:
    """Parses Gemma 4 native tool argument body `{k:<|"|>v<|"|>,n:1,b:false}` into a Python dict."""
    raw_arg_body = raw_arg_body.strip()
    if not raw_arg_body:
        return {}

    result: Dict[str, Any] = {}
    pattern = re.compile(
        r'([A-Za-z_][A-Za-z0-9_]*):(?:<\|"\|>(.*?)<\|"\|>|true|false|-?\d+(?:\.\d+)?)',
        flags=re.DOTALL,
    )
    pos = 0
    for m in re.finditer(
        r'([A-Za-z_][A-Za-z0-9_]*):(<\|"\|>.*?<\|"\|>|true|false|-?\d+(?:\.\d+)?)',
        raw_arg_body,
        flags=re.DOTALL,
    ):
        key = m.group(1)
        val_token = m.group(2)
        if val_token.startswith('<|"|>') and val_token.endswith('<|"|>'):
            result[key] = val_token[5:-5]
        elif val_token == "true":
            result[key] = True
        elif val_token == "false":
            result[key] = False
        elif "." in val_token:
            result[key] = float(val_token)
        else:
            result[key] = int(val_token)
        pos = m.end()

    if pos == 0:
        raise ValueError(f"Malformed Gemma 4 tool argument string: {raw_arg_body!r}")
    return result


def verify_raw_gemma4_completion(completion_text: str) -> Tuple[bool, str]:
    """Simulates vLLM's gemma4 reasoning_parser and tool_call_parser on a raw completion string."""
    if "<start_of_turn>" in completion_text or "<|call_end|>" in completion_text:
        return False, "Contains legacy Gemma 2/3 tags"

    thought_match = GEMMA4_THOUGHT_REGEX.search(completion_text)
    if not thought_match:
        return False, "Missing <|channel>thought...<channel|> block"

    tc_match = GEMMA4_TOOL_CALL_REGEX.search(completion_text)
    if not tc_match:
        return False, "Missing or unclosed <|tool_call>call:name{...}<tool_call|> block"

    tool_name = tc_match.group(1)
    if tool_name not in VALID_TOOL_NAMES:
        return False, f"Unknown tool name '{tool_name}'"

    try:
        args = parse_gemma4_tool_arguments(tc_match.group(2))
    except Exception as exc:
        return False, f"Argument parse error: {exc}"

    missing = REQUIRED_PARAMS[tool_name] - set(args.keys())
    if missing:
        return False, f"Tool '{tool_name}' missing required args: {sorted(missing)}"

    return True, f"{tool_name}({list(args.keys())})"


def run_live_vllm_smoke_test(
    api_base: str,
    model_name: str,
    prompts_path: Path,
    system_md_path: Path,
) -> bool:
    system_prompt = system_md_path.read_text(encoding="utf-8")
    prompts: List[Dict[str, Any]] = [
        json.loads(line)
        for line in prompts_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][:20]

    if len(prompts) < 20:
        print(f"[FAIL] Expected 20 held-out smoke test prompts, found {len(prompts)}")
        return False

    passed = 0
    for idx, item in enumerate(prompts, 1):
        messages = [{"role": "system", "content": system_prompt}] + item["messages"]
        payload = {
            "model": model_name,
            "messages": messages,
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
            print(f"[FAIL] Prompt {idx:02d}/20 HTTP request failed: {exc}")
            return False

        choice = body["choices"][0]
        msg = choice["message"]
        tool_calls = msg.get("tool_calls") or []
        raw_content = msg.get("content") or ""

        # Check that vLLM gemma4 parser extracted tool_calls cleanly without leaking raw tags
        if not tool_calls or "<|tool_call>" in raw_content:
            print(
                f"[FAIL] Prompt {idx:02d}/20: vLLM gemma4 parser failed to extract tool call! "
                f"content={raw_content[:200]!r}"
            )
            return False

        tc = tool_calls[0]["function"]
        t_name = tc["name"]
        try:
            t_args = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else tc["arguments"]
        except Exception as exc:
            print(f"[FAIL] Prompt {idx:02d}/20: Invalid JSON in tool arguments: {exc}")
            return False

        if t_name not in VALID_TOOL_NAMES or not isinstance(t_args, dict):
            print(f"[FAIL] Prompt {idx:02d}/20: Undeclared tool or non-dict args: {t_name}({t_args})")
            return False

        missing = REQUIRED_PARAMS[t_name] - set(t_args.keys())
        if missing:
            print(f"[FAIL] Prompt {idx:02d}/20: Missing required args {sorted(missing)} for {t_name}")
            return False

        passed += 1
        print(f"[OK] Prompt {idx:02d}/20 ({item.get('archetype', 'turn')}) -> {t_name}({list(t_args.keys())})")

    print(f"\n=== VLLM SMOKE TEST PASSED: {passed}/20 (100.0%) clean tool call parses ===")
    return True


def run_offline_parser_self_test(system_md_path: Path) -> bool:
    """Verifies that 20 held-out SFT completions pass the exact vLLM gemma4 parser grammar."""
    from build_gemma4_sft import (
        CanonicalGemma4OfflineTokenizer,
        build_prefix_masked_sample,
        generate_reference_trajectories,
        select_trajectory_target_turns,
    )

    system_prompt = system_md_path.read_text(encoding="utf-8")
    tokenizer = CanonicalGemma4OfflineTokenizer()
    trajs = generate_reference_trajectories(system_prompt, count=6)

    tested = 0
    for traj in trajs:
        msgs = traj["messages"]
        for turn_idx, arch in select_trajectory_target_turns(msgs, max_turns_per_traj=4):
            sample = build_prefix_masked_sample(tokenizer, msgs, turn_idx)
            assert sample is not None
            ok, detail = verify_raw_gemma4_completion(sample["completion_text"])
            if not ok:
                print(f"[FAIL] Self-test prompt {tested + 1}/20 ({arch}): {detail}")
                return False
            tested += 1
            print(f"[OK] Offline Parser Gate {tested:02d}/20 ({arch}) -> {detail}")
            if tested >= 20:
                break
        if tested >= 20:
            break

    assert tested == 20, f"Expected 20 tested prompts, got {tested}"
    print("\n=== OFFLINE GEMMA4 PARSER GATE PASSED: 20/20 (100.0%) ===")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run 20-prompt vLLM gemma4 tool-call parser smoke test."
    )
    repo_root = Path(__file__).resolve().parent.parent.parent
    parser.add_argument(
        "--api-base",
        type=str,
        default="http://127.0.0.1:8000/v1",
        help="OpenAI-compatible vLLM endpoint URL.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="main_lora",
        help="Served model or LoRA adapter name registered in vLLM.",
    )
    parser.add_argument(
        "--prompts",
        type=Path,
        default=repo_root / "data" / "smoke_20.jsonl",
        help="Path to 20 held-out SWE prompts JSONL.",
    )
    parser.add_argument(
        "--system-md",
        type=Path,
        default=repo_root / "src" / "prompts" / "system.md",
        help="Path to src/prompts/system.md.",
    )
    parser.add_argument(
        "--offline-self-test",
        action="store_true",
        help="Run offline vLLM gemma4 parser verification on 20 generated completions.",
    )
    args = parser.parse_args()

    if args.offline_self_test:
        ok = run_offline_parser_self_test(args.system_md)
    else:
        ok = run_live_vllm_smoke_test(args.api_base, args.model, args.prompts, args.system_md)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
