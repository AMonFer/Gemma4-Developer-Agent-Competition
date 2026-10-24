#!/usr/bin/env python3
"""Validation script for Google Gemma 4 Developer Agent competition submissions.

Checks that the submission directory adheres to the official ADK submission contract
and team Dual-Track invariants:
- Exactly one root config (agent.yaml / agent.yml).
- Base model declared is in ALLOWED_MODELS.
- Zero curly braces ({ or }) in prompts/*.md and prompt byte size within track ceiling
  (<= 4,096 bytes for E4B/E2B; <= 8,192 bytes for 31B/12B).
- When `adapter:` is declared in agent.yaml, verifies PEFT adapter_config.json and
  adapter_model.safetensors are sanitized (auto_mapping=null, explicit target_modules list,
  zero vision_tower/audio_tower/PLE tensors).
- All !include directives resolve to existing files within the submission root.
- No forbidden files or extensions (.pt, .bin, root agent.py, etc.).
- Total unpacked size < 3 GiB.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
from pathlib import Path

# Maximum uncompressed submission size: 3 GiB
MAX_TOTAL_SIZE_BYTES = 3 * 1024 * 1024 * 1024
ALLOWED_MODELS = {
    "gemma-4-31b-it-qat-w4a16-ct",
    "gemma-4-12b-it-qat-w4a16-ct",
    "gemma-4-e4b-it-qat-w4a16-ct",
    "gemma-4-e2b-it-qat-w4a16-ct",
}
ROOT_CONFIG_NAMES = ["agent.yaml", "agent.yml", "root_agent.yaml", "root_agent.yml"]
ALLOWED_EXTENSIONS = {
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".json",
    ".safetensors",
    ".gitkeep",
    ".py",  # Only allowed in skills/
}
FORBIDDEN_ADAPTER_SUBSTRINGS = (
    "vision_tower",
    "audio_tower",
    "multi_modal_projector",
    "embed_tokens_per_layer",
    "per_layer",
    "unsloth",
)


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def log_ok(msg: str) -> None:
    print(f"{Colors.GREEN}[OK]{Colors.RESET} {msg}")


def log_warn(msg: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.RESET} {msg}")


def log_err(msg: str) -> None:
    print(f"{Colors.RED}[FAIL]{Colors.RESET} {msg}")


def log_info(msg: str) -> None:
    print(f"{Colors.CYAN}[INFO]{Colors.RESET} {msg}")


def check_submission_root(src_dir: Path) -> tuple[Path | None, list[str]]:
    errors = []
    root_configs = [f for f in ROOT_CONFIG_NAMES if (src_dir / f).is_file()]

    if len(root_configs) == 0:
        errors.append(f"No root agent config found. Expected one of: {ROOT_CONFIG_NAMES}")
        return None, errors
    elif len(root_configs) > 1:
        errors.append(f"Multiple root configs found: {root_configs}. Exactly one must exist.")
        return None, errors

    root_config = src_dir / root_configs[0]
    return root_config, errors


def check_file_extensions(src_dir: Path) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []
    total_size = 0

    for root, _, files in os.walk(src_dir):
        for file in files:
            path = Path(root) / file
            rel_path = path.relative_to(src_dir)
            ext = path.suffix.lower()
            file_size = path.stat().st_size
            total_size += file_size

            # Check root python scripts
            if ext == ".py" and path.parent == src_dir:
                errors.append(
                    f"Forbidden file '{rel_path}': ADK submissions must be declarative YAML/Markdown. "
                    "Entrypoint Python scripts in root are disallowed."
                )

            # Check python scripts outside skills
            if ext == ".py":
                parts = rel_path.parts
                if len(parts) == 0 or parts[0] != "skills":
                    warnings.append(
                        f"Python script '{rel_path}' is outside 'skills/'. "
                        "Python code is only allowed inside skill directories."
                    )

            if ext not in ALLOWED_EXTENSIONS and not file.startswith("."):
                errors.append(
                    f"Forbidden file extension for '{rel_path}' ({ext}). "
                    f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
                )

    if total_size >= MAX_TOTAL_SIZE_BYTES:
        errors.append(
            f"Total uncompressed size exceeds 3 GiB limit: {total_size / (1024**3):.2f} GiB"
        )

    return errors, warnings


def check_prompts_invariants(src_dir: Path, declared_model: str | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    prompts_dir = src_dir / "prompts"
    if not prompts_dir.is_dir():
        return errors, warnings

    is_small_track = bool(declared_model and ("e4b" in declared_model or "e2b" in declared_model))
    byte_ceiling = 4096 if is_small_track else 8192

    for md_file in prompts_dir.glob("*.md"):
        raw_bytes = md_file.read_bytes()
        text = raw_bytes.decode("utf-8")
        if "{" in text or "}" in text:
            errors.append(
                f"Prompt '{md_file.relative_to(src_dir)}' contains curly braces '{{' or '}}'. "
                "ADK interpolates session state variables and raises a fatal KeyError."
            )
        else:
            log_ok(f"Prompt '{md_file.name}' has 0 curly braces.")

        if len(raw_bytes) > byte_ceiling:
            errors.append(
                f"Prompt '{md_file.relative_to(src_dir)}' size ({len(raw_bytes)} bytes) exceeds "
                f"track ceiling ({byte_ceiling} bytes)."
            )
        else:
            log_ok(f"Prompt '{md_file.name}' size is {len(raw_bytes):,} / {byte_ceiling:,} bytes.")

    return errors, warnings


def check_adapters_integrity(
    root_config: Path, src_dir: Path, declared_model: str | None
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    content = root_config.read_text(encoding="utf-8")
    adapter_match = re.search(r"^\s*adapter:\s*([^\s#]+)", content, re.MULTILINE)
    if not adapter_match:
        log_info("Mode: Phase 1 Prompt-Only (no 'adapter:' key in root config).")
        return errors, warnings

    adapter_name = adapter_match.group(1).strip("'\"")
    if declared_model == "gemma-4-31b-it-qat-w4a16-ct":
        errors.append(
            "Track 1 Rule Violation: LoRA adapters must NOT be enabled on 31B "
            "(shrinks vLLM KV cache from 45.5k to 27.7k tokens)."
        )
        return errors, warnings

    adapter_dir = src_dir / "adapters" / adapter_name
    cfg_path = adapter_dir / "adapter_config.json"
    weights_path = adapter_dir / "adapter_model.safetensors"

    if not cfg_path.is_file():
        errors.append(f"Declared adapter '{adapter_name}' is missing '{cfg_path.relative_to(src_dir)}'.")
        return errors, warnings
    if not weights_path.is_file():
        errors.append(f"Declared adapter '{adapter_name}' is missing '{weights_path.relative_to(src_dir)}'.")
        return errors, warnings

    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"Invalid JSON in '{cfg_path.relative_to(src_dir)}': {exc}")
        return errors, warnings

    if cfg.get("auto_mapping") is not None:
        errors.append(
            f"Unsanitized adapter_config.json: 'auto_mapping' must be null, found {cfg.get('auto_mapping')}."
        )

    expected_base = f"google/{declared_model}" if declared_model else None
    actual_base = cfg.get("base_model_name_or_path", "")
    if expected_base and actual_base not in {expected_base, declared_model}:
        errors.append(
            f"Adapter base_model_name_or_path '{actual_base}' does not match '{expected_base}'."
        )

    target_mods = cfg.get("target_modules")
    if not isinstance(target_mods, list) or not target_mods:
        errors.append(
            f"Adapter target_modules must be a non-empty list of module names (not regex): {target_mods!r}"
        )
    else:
        raw_cfg_str = json.dumps(cfg)
        for bad in FORBIDDEN_ADAPTER_SUBSTRINGS:
            if bad in raw_cfg_str:
                errors.append(f"Forbidden substring '{bad}' found in adapter_config.json.")

    # Inspect safetensors header without loading full weights into memory
    try:
        with open(weights_path, "rb") as f:
            header_size = struct.unpack("<Q", f.read(8))[0]
            header = json.loads(f.read(header_size).decode("utf-8"))
        tensor_keys = [k for k in header.keys() if k != "__metadata__"]
        if not tensor_keys:
            errors.append(f"No tensors found in '{weights_path.relative_to(src_dir)}'.")
        for k in tensor_keys:
            if any(bad in k for bad in FORBIDDEN_ADAPTER_SUBSTRINGS):
                errors.append(f"Forbidden non-decoder tensor '{k}' found in adapter_model.safetensors.")
                break
        log_ok(
            f"Sanitized PEFT adapter '{adapter_name}' verified ({len(tensor_keys)} tensors, "
            f"{weights_path.stat().st_size / (1024*1024):.2f} MB)."
        )
    except Exception as exc:
        errors.append(f"Failed to inspect safetensors header in '{weights_path.relative_to(src_dir)}': {exc}")

    return errors, warnings


def check_includes_and_model(
    root_config: Path, src_dir: Path
) -> tuple[str | None, list[str], list[str]]:
    errors = []
    warnings = []
    declared_model: str | None = None
    include_regex = re.compile(r"!include\s+([^\s#]+)")
    model_regex = re.compile(r"^\s*model:\s*([^\s#]+)", re.MULTILINE)

    # Check model in root config
    try:
        content = root_config.read_text(encoding="utf-8")
        model_match = model_regex.search(content)
        if not model_match:
            warnings.append(f"No explicit 'model:' key found in '{root_config.name}'.")
        else:
            declared_model = model_match.group(1).strip("'\"")
            if declared_model not in ALLOWED_MODELS:
                errors.append(
                    f"Model '{declared_model}' is not in official ALLOWED_MODELS {sorted(ALLOWED_MODELS)}."
                )
            else:
                log_ok(f"Declared model is valid: '{declared_model}'")
    except Exception as e:
        errors.append(f"Failed to read root config '{root_config}': {e}")

    # Traverse all YAML files to verify !include references
    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.endswith((".yaml", ".yml")):
                yaml_path = Path(root) / file
                try:
                    text = yaml_path.read_text(encoding="utf-8")
                except Exception as e:
                    errors.append(f"Could not read YAML '{yaml_path.relative_to(src_dir)}': {e}")
                    continue

                for match in include_regex.finditer(text):
                    inc_path_str = match.group(1).strip("'\"")

                    if inc_path_str.startswith(("/", "\\")):
                        errors.append(
                            f"Absolute !include path in '{yaml_path.relative_to(src_dir)}': '{inc_path_str}'. "
                            "Paths must be relative."
                        )
                        continue

                    target = (yaml_path.parent / inc_path_str).resolve()
                    try:
                        target.relative_to(src_dir.resolve())
                    except ValueError:
                        errors.append(
                            f"Path traversal detected in '{yaml_path.relative_to(src_dir)}': "
                            f"!include '{inc_path_str}' escapes submission root."
                        )
                        continue

                    if not target.exists():
                        errors.append(
                            f"Missing included file in '{yaml_path.relative_to(src_dir)}': "
                            f"'{inc_path_str}' (resolved to '{target}') does not exist."
                        )

    return declared_model, errors, warnings


def validate(src_dir: Path) -> bool:
    print(f"\n{Colors.BOLD}=== Validating Gemma 4 Agent Submission Directory ==={Colors.RESET}")
    log_info(f"Target directory: {src_dir.resolve()}")

    if not src_dir.is_dir():
        log_err(f"Directory '{src_dir}' does not exist.")
        return False

    all_errors: list[str] = []
    all_warnings: list[str] = []

    # 1. Root config
    root_config, root_errors = check_submission_root(src_dir)
    all_errors.extend(root_errors)
    if root_config:
        log_ok(f"Found root config: '{root_config.name}'")

    # 2. Extensions & size
    ext_errors, ext_warnings = check_file_extensions(src_dir)
    all_errors.extend(ext_errors)
    all_warnings.extend(ext_warnings)

    # 3. Includes, Model, Prompt Invariants & PEFT Adapter Checks
    if root_config:
        declared_model, inc_errors, inc_warnings = check_includes_and_model(root_config, src_dir)
        all_errors.extend(inc_errors)
        all_warnings.extend(inc_warnings)

        p_errors, p_warnings = check_prompts_invariants(src_dir, declared_model)
        all_errors.extend(p_errors)
        all_warnings.extend(p_warnings)

        a_errors, a_warnings = check_adapters_integrity(root_config, src_dir, declared_model)
        all_errors.extend(a_errors)
        all_warnings.extend(a_warnings)

    print()
    for w in all_warnings:
        log_warn(w)

    if all_errors:
        print(f"\n{Colors.RED}{Colors.BOLD}Validation failed with {len(all_errors)} error(s):{Colors.RESET}")
        for err in all_errors:
            log_err(err)
        return False

    log_ok("All validation checks passed cleanly!")
    return True


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent.parent
    src_path = repo_root / "src"

    if len(sys.argv) > 1:
        src_path = Path(sys.argv[1])

    success = validate(src_path)
    sys.exit(0 if success else 1)
