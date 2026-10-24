#!/usr/bin/env python3
"""Packaging script to create a hermetic submission.zip for Kaggle.

Ensures:
- Pre-validation passes cleanly before creating the zip.
- Phase 1 (Prompt-Only) / Track 1: Strictly packages the 4 core files
  (agent.yaml, eval_config.yaml, configs/sampling.yaml, prompts/system.md).
- Phase 2 (Adapted PEFT LoRA): Strictly packages the 6 files
  (the 4 core files + adapters/<name>/adapter_config.json + adapters/<name>/adapter_model.safetensors).
- Excludes junk files (.DS_Store, __pycache__, README.md inside adapters/, .gitkeep, etc.).
- Calculates SHA256 hash and size metrics.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import zipfile
from pathlib import Path

from validate import validate

EXCLUDED_FILENAMES = {".DS_Store", "Thumbs.db", ".gitkeep", "README.md"}
EXCLUDED_DIRNAMES = {"__pycache__", ".git", ".pytest_cache"}
EXCLUDED_EXTENSIONS = {".pyc", ".pyo", ".tmp"}


def sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def detect_declared_adapter(src_dir: Path) -> str | None:
    root_yaml = src_dir / "agent.yaml"
    if not root_yaml.is_file():
        return None
    content = root_yaml.read_text(encoding="utf-8")
    m = re.search(r"^\s*adapter:\s*([^\s#]+)", content, re.MULTILINE)
    return m.group(1).strip("'\"") if m else None


def package(src_dir: Path, output_zip: Path, skip_validation: bool = False) -> bool:
    if not skip_validation:
        if not validate(src_dir):
            print("\n[ERROR] Packaging aborted because validation failed.")
            return False

    declared_adapter = detect_declared_adapter(src_dir)
    expected_file_count = 6 if declared_adapter else 4

    print(f"\nPackaging '{src_dir}' into '{output_zip}'...")
    print(
        f"Submission Profile: {'Phase 2 Adapted PEFT LoRA (' + declared_adapter + ', 6 files)' if declared_adapter else 'Phase 1 Prompt-Only (4 files)'}"
    )
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    if output_zip.exists():
        output_zip.unlink()

    total_files = 0
    total_uncompressed_bytes = 0

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRNAMES)
            rel_root = Path(root).relative_to(src_dir)

            # Only include adapters/<declared_adapter> when an adapter is explicitly declared in agent.yaml
            if rel_root.parts and rel_root.parts[0] == "adapters":
                if not declared_adapter:
                    continue
                if len(rel_root.parts) >= 2 and rel_root.parts[1] != declared_adapter:
                    continue

            for file in sorted(files):
                if file in EXCLUDED_FILENAMES:
                    continue
                ext = Path(file).suffix.lower()
                if ext in EXCLUDED_EXTENSIONS:
                    continue

                full_path = Path(root) / file
                rel_path = full_path.relative_to(src_dir)
                arcname = str(rel_path).replace("\\", "/")

                zf.write(full_path, arcname=arcname)
                total_files += 1
                total_uncompressed_bytes += full_path.stat().st_size

    compressed_bytes = output_zip.stat().st_size
    checksum = sha256_file(output_zip)

    print("\n=== Submission Package Created Successfully ===")
    print(f"Destination:          {output_zip.resolve()}")
    print(f"Total files packed:   {total_files} (expected: {expected_file_count})")
    print(
        f"Uncompressed size:    {total_uncompressed_bytes / (1024*1024):.2f} MB ({total_uncompressed_bytes:,} bytes)"
    )
    print(f"Compressed zip size:  {compressed_bytes / (1024*1024):.2f} MB ({compressed_bytes:,} bytes)")
    print(f"SHA256 Checksum:      {checksum}")

    with zipfile.ZipFile(output_zip, "r") as zf:
        namelist = zf.namelist()
        print("Archived files:", namelist)
        if "agent.yaml" not in namelist and "agent.yml" not in namelist:
            print("  -> ERROR: Missing root agent config in archive root!")
            return False
        if total_files != expected_file_count:
            print(
                f"  -> ERROR: Archive contains {total_files} files, expected strictly {expected_file_count}!"
            )
            return False

    return True


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent.parent
    src_path = repo_root / "src"
    dist_zip = repo_root / "dist" / "submission.zip"

    skip_val = "--no-validate" in sys.argv
    success = package(src_path, dist_zip, skip_validation=skip_val)
    sys.exit(0 if success else 1)
