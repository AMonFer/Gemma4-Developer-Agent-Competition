#!/usr/bin/env python3
"""Validation script for Google Gemma 4 Developer Agent competition submissions.

Checks that the submission directory adheres to the official ADK submission contract:
- Exactly one root config (agent.yaml / agent.yml).
- Base model declared is gemma-4-31b-it-qat-w4a16-ct.
- All !include directives resolve to existing files within the submission root.
- No forbidden files or extensions (.pt, .bin, root agent.py, etc.).
- Total unpacked size < 3 GiB.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Maximum uncompressed submission size: 3 GiB
MAX_TOTAL_SIZE_BYTES = 3 * 1024 * 1024 * 1024
ALLOWED_MODEL = "gemma-4-31b-it-qat-w4a16-ct"
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


def check_includes_and_model(root_config: Path, src_dir: Path) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []
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
            if declared_model != ALLOWED_MODEL:
                warnings.append(
                    f"Model '{declared_model}' differs from competition requirement '{ALLOWED_MODEL}'. "
                    f"Make sure to use '{ALLOWED_MODEL}' before submitting to Kaggle."
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

                    # Check for absolute paths
                    if inc_path_str.startswith(("/", "\\")):
                        errors.append(
                            f"Absolute !include path in '{yaml_path.relative_to(src_dir)}': '{inc_path_str}'. "
                            "Paths must be relative."
                        )
                        continue

                    # Check for path traversal escaping src_dir
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

    return errors, warnings


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

    # 3. Includes & Model
    if root_config:
        inc_errors, inc_warnings = check_includes_and_model(root_config, src_dir)
        all_errors.extend(inc_errors)
        all_warnings.extend(inc_warnings)

    # Print summary
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
