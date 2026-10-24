#!/usr/bin/env python3
"""Component B: Template-Correct Gemma 4 E4B SFT Dataset Builder with Prefix-Stable Loss Masking.

Constructs multi-turn prefix-completion SFT samples using the official Gemma 4 chat template:
  - Tokenizer: google/gemma-4-e4b-it (or unsloth/gemma-4-e4b-it)
  - Native Gemma 4 completion format:
      <|turn>model
      <|channel>thought
      ...
      <channel|><|tool_call>call:tool_name{...}<tool_call|><turn|>
  - Deterministic prefix stability assertions:
      assert full_text.startswith(prompt_text)
      assert full_ids[:prefix_len] == prompt_ids[:prefix_len]
  - Prefix-stable loss masking:
      labels = [-100] * prefix_len + full_ids[prefix_len:]
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Exact 6-tool schema matching swegemma/tools/workspace.py & execution.py
OFFICIAL_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command in /bin/bash -c inside /workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command string to execute in /workspace.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from /workspace with 1-indexed inclusive line slicing (max 150 lines / 10000 chars).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to file relative to /workspace.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "1-indexed start line (inclusive).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "1-indexed end line (inclusive).",
                    },
                },
                "required": ["filepath", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace old_string with new_string in an existing file inside /workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to file relative to /workspace.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact existing lines to replace (include 2-4 context lines).",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement lines preserving exact indentation.",
                    },
                    "allow_multiple": {
                        "type": "boolean",
                        "description": "Whether to replace multiple occurrences (default false).",
                    },
                },
                "required": ["filepath", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file at /workspace/<filepath>.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to file relative to /workspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full text content to write.",
                    },
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_similar_code",
            "description": "Find top-k functions/classes with highest cosine or lexical similarity to query in the repository symbol graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Symbol name or dotted path (e.g. 'Class.method' or 'func_name').",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of results to return (default 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_neighbors",
            "description": "Find callers and callees of a symbol node in the repository call graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Symbol node name (e.g. 'pkg.module.Class.method' or 'Class.method').",
                    },
                    "edge_type": {
                        "type": "string",
                        "description": "Optional edge type filter (e.g. 'calls').",
                    },
                    "max_neighbors": {
                        "type": "integer",
                        "description": "Maximum neighbors to return (default 50).",
                    },
                },
                "required": ["node"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "Return live tool call budget, remaining time, and patch submission status.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_patch",
            "description": "Capture git diff from /workspace and mark the task patch as submitted.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def format_gemma4_tool_dict(args: Dict[str, Any]) -> str:
    """Formats a tool argument dict into Gemma 4 native tool call syntax: {k:<|\"|>v<|\"|>,n:1}."""
    parts: List[str] = []
    for key in sorted(args.keys()):
        val = args[key]
        if isinstance(val, bool):
            parts.append(f"{key}:{'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            parts.append(f"{key}:{val}")
        elif isinstance(val, str):
            parts.append(f'{key}:<|"|>{val}<|"|>')
        else:
            parts.append(f"{key}:{json.dumps(val, ensure_ascii=False)}")
    return "{" + ",".join(parts) + "}"


class CanonicalGemma4OfflineTokenizer:
    """Deterministic fallback tokenizer implementing the exact Gemma 4 chat template & special tokens.

    Used when running offline unit tests (`--offline-tokenizer`) on hosts without cached
    Hugging Face `google/gemma-4-e4b-it` weights. In production/Kaggle dataset generation,
    `AutoTokenizer.from_pretrained("google/gemma-4-e4b-it")` is used directly.
    """

    SPECIAL_TOKENS = [
        "<bos>",
        "<|turn>",
        "<turn|>",
        "<|channel>",
        "<channel|>",
        "<|tool>",
        "<tool|>",
        "<|tool_call>",
        "<tool_call|>",
        "<|tool_response>",
        "<tool_response|>",
        '<|"|>',
    ]

    def apply_chat_template(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tokenize: bool = False,
        add_generation_prompt: bool = False,
        enable_thinking: bool = True,
    ) -> Any:
        out: List[str] = ["<bos>"]
        idx = 0

        # System turn with tool declarations
        if messages and messages[0].get("role") == "system":
            sys_content = messages[0].get("content", "").strip()
            out.append("<|turn>system\n")
            if enable_thinking:
                out.append("<|think|>\n")
            if tools:
                for t in tools:
                    fn = t.get("function", t)
                    out.append(f"<|tool>{json.dumps(fn, ensure_ascii=False)}<tool|>")
                out.append("\n")
            out.append(f"{sys_content}<turn|>\n")
            idx = 1

        while idx < len(messages):
            msg = messages[idx]
            role = msg.get("role", "")
            if role == "user":
                out.append(f"<|turn>user\n{msg.get('content', '')}<turn|>\n")
            elif role == "assistant":
                thought = (
                    msg.get("thinking")
                    or msg.get("reasoning_content")
                    or "Analyzing repository state and executing the next step."
                )
                out.append("<|turn>model\n")
                out.append(f"<|channel>thought\n{thought.strip()}\n<channel|>")
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", tc)
                    t_name = fn.get("name", "")
                    t_args = fn.get("arguments", {})
                    if isinstance(t_args, str):
                        t_args = json.loads(t_args)
                    out.append(
                        f"<|tool_call>call:{t_name}{format_gemma4_tool_dict(t_args)}<tool_call|>"
                    )
                if msg.get("content"):
                    out.append(str(msg["content"]))
                out.append("<turn|>\n")
            elif role == "tool":
                t_name = msg.get("name", "tool")
                t_content = msg.get("content", "")
                out.append(
                    f"<|turn>user\n<|tool_response>response:{t_name}{t_content}<tool_response|><turn|>\n"
                )
            idx += 1

        if add_generation_prompt:
            out.append("<|turn>model\n")

        rendered = "".join(out)
        if tokenize:
            return self.encode(rendered, add_special_tokens=False)
        return rendered

    def encode(self, text: str, add_special_tokens: bool = False) -> List[int]:
        # Tokenize splitting on special tokens and whitespace/punctuation boundaries so
        # prefix boundaries are never merged across special tags.
        pattern = (
            r"(<bos>|<\|turn>|<turn\|>|<\|channel>|<channel\|>|<\|tool>|<tool\|>"
            r"|<\|tool_call>|<tool_call\|>|<\|tool_response>|<tool_response\|>|<\|\"\|>|\n|\s+|\w+|[^\w\s])"
        )
        pieces = re.findall(pattern, text)
        ids: List[int] = []
        for p in pieces:
            # Deterministic stable positive integer ID in [2, 255999]
            h = 2166136261
            for b in p.encode("utf-8"):
                h = ((h ^ b) * 16777619) & 0xFFFFFFFF
            ids.append(2 + (h % 250000))
        return ids


def load_gemma4_tokenizer(model_name: str, offline_fallback: bool = False) -> Any:
    """Loads the official Gemma 4 tokenizer from Hugging Face (or fallback when offline_fallback=True)."""
    if offline_fallback:
        print("[INFO] Using deterministic CanonicalGemma4OfflineTokenizer (--offline-tokenizer).")
        return CanonicalGemma4OfflineTokenizer()

    from transformers import AutoTokenizer

    print(f"[INFO] Loading official tokenizer: {model_name}")
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    return tokenizer


def classify_assistant_turn(msg: Dict[str, Any], assist_turn_num: int) -> Optional[str]:
    """Classifies an assistant tool-call turn into one of 4 high-value SFT archetypes."""
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return None
    fn = tool_calls[0].get("function", {})
    name = fn.get("name", "")
    args = fn.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}

    if name in {"edit_file", "write_file"}:
        return "surgical_edit"
    if name == "submit_patch":
        return "hygiene_submit"
    if name == "read_file" and assist_turn_num <= 3:
        return "localization"
    if name == "run_command":
        cmd = str(args.get("command", ""))
        if "git grep" in cmd and assist_turn_num <= 2:
            return "localization"
        if "/tmp/repro.py" in cmd or "py_compile" in cmd or "pytest" in cmd or "/tmp/edit.py" in cmd:
            return "repro_or_verify"
        if "git checkout --" in cmd and "git status" in cmd:
            return "hygiene_submit"
    return None


def select_trajectory_target_turns(messages: List[Dict[str, Any]], max_turns_per_traj: int = 4) -> List[Tuple[int, str]]:
    """Selects up to 4 high-leverage assistant turns per trajectory across the 4 archetypes."""
    by_archetype: Dict[str, List[int]] = {
        "localization": [],
        "repro_or_verify": [],
        "surgical_edit": [],
        "hygiene_submit": [],
    }
    assist_idx = 0
    for idx, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            assist_idx += 1
            arch = classify_assistant_turn(msg, assist_idx)
            if arch:
                by_archetype[arch].append(idx)

    selected: List[Tuple[int, str]] = []
    # 1. Always prioritize surgical_edit (up to 2 per trajectory if multi-hunk)
    for idx in by_archetype["surgical_edit"][:2]:
        selected.append((idx, "surgical_edit"))
    # 2. First localization turn
    if by_archetype["localization"]:
        selected.append((by_archetype["localization"][0], "localization"))
    # 3. First repro_or_verify turn
    if by_archetype["repro_or_verify"]:
        selected.append((by_archetype["repro_or_verify"][0], "repro_or_verify"))
    # 4. Last hygiene_submit turn
    if by_archetype["hygiene_submit"] and len(selected) < max_turns_per_traj:
        selected.append((by_archetype["hygiene_submit"][-1], "hygiene_submit"))

    selected.sort(key=lambda pair: pair[0])
    return selected[:max_turns_per_traj]


def build_prefix_masked_sample(
    tokenizer: Any,
    messages: List[Dict[str, Any]],
    target_turn_idx: int,
    max_seq_length: int = 8192,
) -> Optional[Dict[str, Any]]:
    """Constructs a single prefix-masked training example ending at messages[target_turn_idx].

    Verifies exact character and token prefix alignment between prompt_messages and full_messages,
    masking all prompt tokens (system, tools, user, prior turns, tool responses) with -100.
    """
    prompt_messages = messages[:target_turn_idx]
    full_messages = messages[: target_turn_idx + 1]

    prompt_text: str = tokenizer.apply_chat_template(
        prompt_messages,
        tools=OFFICIAL_TOOLS,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=True,
    )
    full_text: str = tokenizer.apply_chat_template(
        full_messages,
        tools=OFFICIAL_TOOLS,
        tokenize=False,
        add_generation_prompt=False,
        enable_thinking=True,
    )

    # If the tokenizer's generation prompt appends an open '<|channel>thought\n' tag,
    # strip it from prompt_text so that `<|channel>thought\n...\n<channel|>` is supervised in completion.
    for trailing_thought_tag in ("<|channel>thought\n", "<|channel>thought"):
        if prompt_text.endswith(trailing_thought_tag) and full_text.startswith(
            prompt_text[: -len(trailing_thought_tag)] + trailing_thought_tag
        ):
            prompt_text = prompt_text[: -len(trailing_thought_tag)]
            break

    # 1. Hard character-level prefix invariant + native Gemma 4 tag verification
    assert full_text.startswith(prompt_text), (
        f"Prefix mismatch at turn {target_turn_idx}!\n"
        f"Prompt tail: {prompt_text[-120:]!r}\n"
        f"Full slice:  {full_text[max(0, len(prompt_text)-60):len(prompt_text)+120]!r}"
    )

    completion_text = full_text[len(prompt_text) :]
    assert "<|channel>thought" in completion_text and "<channel|>" in completion_text, (
        f"Missing native Gemma 4 thought channel tags in completion: {completion_text[:200]!r}"
    )
    assert "<|tool_call>" in completion_text and "<tool_call|>" in completion_text, (
        f"Missing native Gemma 4 tool_call tags in completion: {completion_text[:200]!r}"
    )
    assert "<start_of_turn>" not in full_text and "<|call_end|>" not in full_text, (
        "FATAL: Legacy Gemma 2/3 tokens (<start_of_turn> or <|call_end|>) detected in formatted text!"
    )

    # 2. Tokenize without adding duplicate BOS tokens
    prompt_ids: List[int] = list(tokenizer.encode(prompt_text, add_special_tokens=False))
    full_ids: List[int] = list(tokenizer.encode(full_text, add_special_tokens=False))

    if len(full_ids) > max_seq_length:
        return None

    prefix_len = len(prompt_ids)
    if full_ids[:prefix_len] != prompt_ids:
        # Handle potential single-token BPE boundary merge at the trailing newline of `<|turn>model\n`
        if prefix_len > 1 and full_ids[: prefix_len - 1] == prompt_ids[:-1]:
            prefix_len -= 1
        else:
            raise AssertionError(
                f"Token-level prefix mismatch at turn {target_turn_idx}: "
                f"full_ids[:{prefix_len}] != prompt_ids"
            )

    assert full_ids[:prefix_len] == prompt_ids[:prefix_len], "Token prefix slice mismatch!"

    labels = [-100] * prefix_len + full_ids[prefix_len:]
    supervised_tokens = sum(1 for tok in labels if tok != -100)
    assert supervised_tokens >= 8, f"Too few supervised completion tokens ({supervised_tokens})"

    return {
        "input_ids": full_ids,
        "attention_mask": [1] * len(full_ids),
        "labels": labels,
        "prompt_text": prompt_text,
        "completion_text": completion_text,
        "num_tokens": len(full_ids),
        "supervised_tokens": supervised_tokens,
    }


def generate_reference_trajectories(system_prompt: str, count: int = 500) -> List[Dict[str, Any]]:
    """Generates canonical multi-turn Python SWE trajectories across fastapi, rich, requests, httpx
    when bootstrapping or running deterministic `--verify` tests before streaming large HF archives.
    """
    from curate_trajectories import (
        PATCH_HYGIENE_CMD,
        build_harness_initial_user_prompt,
        format_tool_response_json,
    )

    blueprints = [
        {
            "repo": "tiangolo/fastapi",
            "pkg": "fastapi",
            "filepath": "fastapi/routing.py",
            "symbol": "serialize_response",
            "problem": (
                "When a route defines `response_model_exclude_unset=True` and returns a Pydantic model "
                "subclass wrapped in a dict, `serialize_response` in `fastapi/routing.py` fails to "
                "forward `exclude_unset` to nested `jsonable_encoder` calls."
            ),
            "hints": "Inspect `serialize_response` around line 140 of `fastapi/routing.py`.",
            "start_line": 130,
            "end_line": 175,
            "file_snippet": (
                "def serialize_response(*, field: Optional[ModelField] = None, response_content: Any, "
                "include: Optional[IncEx] = None, exclude: Optional[IncEx] = None, "
                "by_alias: bool = True, exclude_unset: bool = False) -> Any:\n"
                "    if field:\n"
                "        value, errors_ = field.validate(response_content, {}, loc=(\"response\",))\n"
                "        return jsonable_encoder(value, include=include, exclude=exclude, by_alias=by_alias)\n"
                "    else:\n"
                "        return jsonable_encoder(response_content)\n"
            ),
            "old_str": (
                "        value, errors_ = field.validate(response_content, {}, loc=(\"response\",))\n"
                "        return jsonable_encoder(value, include=include, exclude=exclude, by_alias=by_alias)\n"
                "    else:"
            ),
            "new_str": (
                "        value, errors_ = field.validate(response_content, {}, loc=(\"response\",))\n"
                "        return jsonable_encoder(\n"
                "            value, include=include, exclude=exclude, by_alias=by_alias, exclude_unset=exclude_unset\n"
                "        )\n"
                "    else:"
            ),
            "test_cmd": "python3 -m py_compile fastapi/routing.py && timeout 30 python3 /tmp/repro.py 2>&1 | tail -5; timeout 60 python3 -m pytest tests/test_serialize_response.py -k exclude_unset -x -q -p no:cacheprovider 2>&1 | tail -15",
        },
        {
            "repo": "Textualize/rich",
            "pkg": "rich",
            "filepath": "rich/text.py",
            "symbol": "truncate",
            "problem": (
                "`Text.truncate(max_width, overflow='ellipsis')` raises an IndexError or leaves "
                "trailing spans out of bounds when `max_width == 0`."
            ),
            "hints": "Check `Text.truncate` in `rich/text.py`.",
            "start_line": 710,
            "end_line": 760,
            "file_snippet": (
                "    def truncate(self, max_width: int, *, overflow: Optional[OverflowMethod] = None, pad: bool = False) -> None:\n"
                "        _overflow = overflow or self.overflow or DEFAULT_OVERFLOW\n"
                "        if _overflow != \"ignore\":\n"
                "            length = cell_len(self.plain)\n"
                "            if length > max_width:\n"
                "                self.set_length(max_width)\n"
            ),
            "old_str": (
                "    def truncate(self, max_width: int, *, overflow: Optional[OverflowMethod] = None, pad: bool = False) -> None:\n"
                "        _overflow = overflow or self.overflow or DEFAULT_OVERFLOW"
            ),
            "new_str": (
                "    def truncate(self, max_width: int, *, overflow: Optional[OverflowMethod] = None, pad: bool = False) -> None:\n"
                "        if max_width <= 0:\n"
                "            self._text = [\"\"]\n"
                "            self._spans.clear()\n"
                "            self._length = 0\n"
                "            return\n"
                "        _overflow = overflow or self.overflow or DEFAULT_OVERFLOW"
            ),
            "test_cmd": "python3 -m py_compile rich/text.py && timeout 30 python3 /tmp/repro.py 2>&1 | tail -5; timeout 60 python3 -m pytest tests/test_text.py -k truncate -x -q -p no:cacheprovider 2>&1 | tail -15",
        },
        {
            "repo": "psf/requests",
            "pkg": "requests",
            "filepath": "requests/sessions.py",
            "symbol": "merge_setting",
            "problem": (
                "`merge_setting` in `requests/sessions.py` crashes with `TypeError` when `session_setting` "
                "is a case-insensitive dict and `request_setting` contains `None` keys."
            ),
            "hints": "Look at `merge_setting` in `requests/sessions.py`.",
            "start_line": 50,
            "end_line": 95,
            "file_snippet": (
                "def merge_setting(request_setting, session_setting, dict_class=OrderedDict):\n"
                "    if session_setting is None:\n"
                "        return request_setting\n"
                "    if request_setting is None:\n"
                "        return session_setting\n"
                "    merged_setting = dict_class(to_key_val_list(session_setting))\n"
                "    merged_setting.update(to_key_val_list(request_setting))\n"
                "    none_keys = [k for (k, v) in merged_setting.items() if v is None]\n"
                "    for key in none_keys:\n"
                "        del merged_setting[key]\n"
                "    return merged_setting\n"
            ),
            "old_str": (
                "    none_keys = [k for (k, v) in merged_setting.items() if v is None]\n"
                "    for key in none_keys:\n"
                "        del merged_setting[key]"
            ),
            "new_str": (
                "    none_keys = [k for (k, v) in merged_setting.items() if v is None or k is None]\n"
                "    for key in none_keys:\n"
                "        merged_setting.pop(key, None)"
            ),
            "test_cmd": "python3 -m py_compile requests/sessions.py && timeout 30 python3 /tmp/repro.py 2>&1 | tail -5; timeout 60 python3 -m pytest tests/test_requests.py -k merge_setting -x -q -p no:cacheprovider 2>&1 | tail -15",
        },
        {
            "repo": "encode/httpx",
            "pkg": "httpx",
            "filepath": "httpx/_models.py",
            "symbol": "_normalize_header_value",
            "problem": (
                "Passing an integer or boolean header value to `httpx.Headers` raises a confusing "
                "`AttributeError` instead of coercing or raising a clean `TypeError` in `_normalize_header_value`."
            ),
            "hints": "Inspect `_normalize_header_value` in `httpx/_models.py`.",
            "start_line": 65,
            "end_line": 105,
            "file_snippet": (
                "def _normalize_header_value(value: Union[str, bytes], encoding: Optional[str] = None) -> bytes:\n"
                "    if isinstance(value, bytes):\n"
                "        return value\n"
                "    return value.encode(encoding or \"ascii\")\n"
            ),
            "old_str": (
                "def _normalize_header_value(value: Union[str, bytes], encoding: Optional[str] = None) -> bytes:\n"
                "    if isinstance(value, bytes):\n"
                "        return value\n"
                "    return value.encode(encoding or \"ascii\")"
            ),
            "new_str": (
                "def _normalize_header_value(value: Union[str, bytes], encoding: Optional[str] = None) -> bytes:\n"
                "    if isinstance(value, bytes):\n"
                "        return value\n"
                "    if not isinstance(value, str):\n"
                "        raise TypeError(f\"Header value must be str or bytes, not {type(value).__name__}\")\n"
                "    return value.encode(encoding or \"ascii\")"
            ),
            "test_cmd": "python3 -m py_compile httpx/_models.py && timeout 30 python3 /tmp/repro.py 2>&1 | tail -5; timeout 60 python3 -m pytest tests/models/test_headers.py -x -q -p no:cacheprovider 2>&1 | tail -15",
        },
    ]

    trajectories: List[Dict[str, Any]] = []
    for i in range(count):
        bp = blueprints[i % len(blueprints)]
        line_offset = (i // len(blueprints)) % 15
        start_l = bp["start_line"] + line_offset
        end_l = bp["end_line"] + line_offset
        user_prompt = build_harness_initial_user_prompt(bp["repo"], bp["problem"], bp["hints"])

        grep_args = {"command": f"git grep -n '{bp['symbol']}' | grep -v 'test'"}
        read_args = {"filepath": bp["filepath"], "start_line": start_l, "end_line": end_l}
        repro_args = {
            "command": (
                "cat > /tmp/repro.py << 'PYEOF'\n"
                "import sys\n"
                "sys.path.insert(0, '/workspace')\n"
                "print('REPRO_OK')\n"
                "PYEOF\n"
                "timeout 30 python3 /tmp/repro.py 2>&1 | tail -20"
            )
        }
        edit_args = {
            "filepath": bp["filepath"],
            "old_string": bp["old_str"],
            "new_string": bp["new_str"],
            "allow_multiple": False,
        }
        verify_args = {"command": bp["test_cmd"]}
        hygiene_args = {"command": PATCH_HYGIENE_CMD}

        msgs: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_prompt},
            # Turn 1: Localize via git grep
            {
                "role": "assistant",
                "content": "",
                "thinking": f"Searching {bp['pkg']}/ for {bp['symbol']} to locate the implementation.",
                "reasoning_content": f"Searching {bp['pkg']}/ for {bp['symbol']} to locate the implementation.",
                "tool_calls": [
                    {
                        "id": "call_01",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": grep_args},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "run_command",
                "tool_call_id": "call_01",
                "content": format_tool_response_json(
                    "run_command", grep_args, f"{bp['filepath']}:{start_l + 5}:def {bp['symbol']}"
                ),
            },
            # Turn 2: Read file slice
            {
                "role": "assistant",
                "content": "",
                "thinking": f"Reading lines {start_l}-{end_l} of {bp['filepath']} to inspect {bp['symbol']}.",
                "reasoning_content": f"Reading lines {start_l}-{end_l} of {bp['filepath']} to inspect {bp['symbol']}.",
                "tool_calls": [
                    {
                        "id": "call_02",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": read_args},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": "call_02",
                "content": format_tool_response_json("read_file", read_args, bp["file_snippet"]),
            },
            # Turn 3: Isolated reproduction in /tmp/repro.py
            {
                "role": "assistant",
                "content": "",
                "thinking": "Writing and running an isolated reproduction script in /tmp/repro.py.",
                "reasoning_content": "Writing and running an isolated reproduction script in /tmp/repro.py.",
                "tool_calls": [
                    {
                        "id": "call_03",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": repro_args},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "run_command",
                "tool_call_id": "call_03",
                "content": format_tool_response_json("run_command", repro_args, "REPRO_OK"),
            },
            # Turn 4: Surgical edit
            {
                "role": "assistant",
                "content": "",
                "thinking": f"Applying surgical fix in {bp['filepath']} using exact context lines from read_file.",
                "reasoning_content": f"Applying surgical fix in {bp['filepath']} using exact context lines from read_file.",
                "tool_calls": [
                    {
                        "id": "call_04",
                        "type": "function",
                        "function": {"name": "edit_file", "arguments": edit_args},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "edit_file",
                "tool_call_id": "call_04",
                "content": format_tool_response_json("edit_file", edit_args, ""),
            },
            # Turn 5: Fused compile + repro + targeted pytest verification
            {
                "role": "assistant",
                "content": "",
                "thinking": "Compiling modified module, re-running /tmp/repro.py, and running targeted unit tests.",
                "reasoning_content": "Compiling modified module, re-running /tmp/repro.py, and running targeted unit tests.",
                "tool_calls": [
                    {
                        "id": "call_05",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": verify_args},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "run_command",
                "tool_call_id": "call_05",
                "content": format_tool_response_json("run_command", verify_args, "REPRO_OK\n1 passed in 0.14s"),
            },
            # Turn 6: Patch hygiene
            {
                "role": "assistant",
                "content": "",
                "thinking": "Cleaning scratch files and reverting any protected test/config paths before submitting.",
                "reasoning_content": "Cleaning scratch files and reverting any protected test/config paths before submitting.",
                "tool_calls": [
                    {
                        "id": "call_06",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": hygiene_args},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "run_command",
                "tool_call_id": "call_06",
                "content": format_tool_response_json(
                    "run_command", hygiene_args, f" M {bp['filepath']}\n 1 file changed"
                ),
            },
            # Turn 7: Submit patch
            {
                "role": "assistant",
                "content": "",
                "thinking": f"Only {bp['filepath']} is modified and all checks passed. Calling submit_patch.",
                "reasoning_content": f"Only {bp['filepath']} is modified and all checks passed. Calling submit_patch.",
                "tool_calls": [
                    {
                        "id": "call_07",
                        "type": "function",
                        "function": {"name": "submit_patch", "arguments": {}},
                    }
                ],
            },
        ]
        trajectories.append(
            {
                "instance_id": f"{bp['pkg']}__{bp['symbol']}_{i:04d}",
                "repo": bp["repo"],
                "assistant_turns": 7,
                "weight": 3,
                "messages": msgs,
            }
        )
    return trajectories


def build_sft_dataset(
    trajectories_path: Path,
    system_md_path: Path,
    output_dir: Path,
    model_name: str = "google/gemma-4-e4b-it",
    max_seq_length: int = 8192,
    val_ratio: float = 0.10,
    seed: int = 42,
    offline_tokenizer: bool = False,
    verify: bool = True,
) -> Dict[str, Any]:
    random.seed(seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    system_prompt = system_md_path.read_text(encoding="utf-8")

    if trajectories_path.is_file():
        trajectories = [
            json.loads(line)
            for line in trajectories_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        print(f"[INFO] {trajectories_path} not found; generating reference curated trajectories.")
        trajectories = generate_reference_trajectories(system_prompt, count=500)

    tokenizer = load_gemma4_tokenizer(model_name, offline_fallback=offline_tokenizer)

    samples: List[Dict[str, Any]] = []
    smoke_prompts: List[Dict[str, Any]] = []
    archetype_counts: Dict[str, int] = {}
    repo_counts: Dict[str, int] = {}

    for traj in trajectories:
        messages = traj["messages"]
        repo = traj.get("repo", "unknown")
        instance_id = traj.get("instance_id", "unknown")
        targets = select_trajectory_target_turns(messages, max_turns_per_traj=4)

        for turn_idx, archetype in targets:
            sample = build_prefix_masked_sample(
                tokenizer=tokenizer,
                messages=messages,
                target_turn_idx=turn_idx,
                max_seq_length=max_seq_length,
            )
            if not sample:
                continue

            sample["instance_id"] = instance_id
            sample["repo"] = repo
            sample["archetype"] = archetype
            samples.append(sample)

            archetype_counts[archetype] = archetype_counts.get(archetype, 0) + 1
            repo_counts[repo] = repo_counts.get(repo, 0) + 1

            # Collect 20 diverse held-out prompts (excluding system message, which smoke_test_vllm prepends)
            if len(smoke_prompts) < 20:
                smoke_prompts.append(
                    {
                        "instance_id": instance_id,
                        "repo": repo,
                        "archetype": archetype,
                        "expected_tool": messages[turn_idx]["tool_calls"][0]["function"]["name"],
                        "messages": messages[1:turn_idx],
                    }
                )

    random.shuffle(samples)
    split_idx = int(len(samples) * (1.0 - val_ratio))
    train_samples = samples[:split_idx]
    val_samples = samples[split_idx:]

    # Save JSONL and Parquet for Unsloth SFTTrainer
    train_jsonl = output_dir / "sft_train_e4b.jsonl"
    val_jsonl = output_dir / "sft_val_e4b.jsonl"
    smoke_jsonl = output_dir / "smoke_20.jsonl"
    summary_json = output_dir / "sft_summary_e4b.json"

    for path_out, rows in ((train_jsonl, train_samples), (val_jsonl, val_samples)):
        with open(path_out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(
                    json.dumps(
                        {
                            "instance_id": r["instance_id"],
                            "repo": r["repo"],
                            "archetype": r["archetype"],
                            "input_ids": r["input_ids"],
                            "attention_mask": r["attention_mask"],
                            "labels": r["labels"],
                            "num_tokens": r["num_tokens"],
                            "supervised_tokens": r["supervised_tokens"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    with open(smoke_jsonl, "w", encoding="utf-8") as f:
        for sp in smoke_prompts:
            f.write(json.dumps(sp, ensure_ascii=False) + "\n")

    # Also write Parquet if datasets/pyarrow is available
    try:
        from datasets import Dataset

        Dataset.from_list(
            [
                {
                    "input_ids": r["input_ids"],
                    "attention_mask": r["attention_mask"],
                    "labels": r["labels"],
                }
                for r in train_samples
            ]
        ).to_parquet(str(output_dir / "sft_train_e4b.parquet"))
        Dataset.from_list(
            [
                {
                    "input_ids": r["input_ids"],
                    "attention_mask": r["attention_mask"],
                    "labels": r["labels"],
                }
                for r in val_samples
            ]
        ).to_parquet(str(output_dir / "sft_val_e4b.parquet"))
    except Exception as exc:
        print(f"[INFO] Parquet export skipped ({exc}); JSONL files written cleanly.")

    summary = {
        "tokenizer_model": model_name if not offline_tokenizer else "CanonicalGemma4OfflineTokenizer",
        "max_seq_length": max_seq_length,
        "total_samples": len(samples),
        "train_samples": len(train_samples),
        "val_samples": len(val_samples),
        "smoke_test_prompts": len(smoke_prompts),
        "archetype_distribution": archetype_counts,
        "repo_distribution": repo_counts,
    }
    summary_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    if verify:
        assert len(train_samples) > 0 and len(val_samples) > 0, "Empty train or val split!"
        assert len(smoke_prompts) == 20, f"Expected 20 smoke test prompts, got {len(smoke_prompts)}"
        for s in samples[:50]:
            assert len(s["input_ids"]) == len(s["labels"]) <= max_seq_length
            assert s["labels"][0] == -100, "System/BOS tokens must be masked with -100!"
            assert any(x != -100 for x in s["labels"]), "Sample has zero supervised tokens!"
        print(
            f"[OK] Verified {len(samples)} prefix-masked Gemma 4 SFT samples "
            f"({len(train_samples)} train / {len(val_samples)} val) and 20 smoke prompts."
        )

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build template-correct Gemma 4 E4B SFT dataset with prefix-stable loss masking."
    )
    repo_root = Path(__file__).resolve().parent.parent.parent
    parser.add_argument(
        "--trajectories",
        type=Path,
        default=repo_root / "data" / "curated_trajectories.jsonl",
        help="Input curated trajectories JSONL path.",
    )
    parser.add_argument(
        "--system-md",
        type=Path,
        default=repo_root / "src" / "prompts" / "system.md",
        help="Path to src/prompts/system.md.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repo_root / "data",
        help="Output directory for SFT dataset files.",
    )
    parser.add_argument(
        "--model-name",
        type=str,
        default="google/gemma-4-e4b-it",
        help="Hugging Face tokenizer model ID.",
    )
    parser.add_argument("--max-seq-length", type=int, default=8192)
    parser.add_argument("--val-ratio", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--offline-tokenizer",
        action="store_true",
        help="Use deterministic CanonicalGemma4OfflineTokenizer for local CI verification.",
    )
    parser.add_argument("--verify", action="store_true", default=True)
    args = parser.parse_args()

    build_sft_dataset(
        trajectories_path=args.trajectories,
        system_md_path=args.system_md,
        output_dir=args.output_dir,
        model_name=args.model_name,
        max_seq_length=args.max_seq_length,
        val_ratio=args.val_ratio,
        seed=args.seed,
        offline_tokenizer=args.offline_tokenizer,
        verify=args.verify,
    )


if __name__ == "__main__":
    main()
