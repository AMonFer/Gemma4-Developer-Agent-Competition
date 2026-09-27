#!/usr/bin/env python3
"""Packaging script to create a hermetic submission.zip for Kaggle.

Ensures:
- Pre-validation passes cleanly before creating the zip.
- Files from 'src/' are placed directly in the zip root (agent.yaml at root).
- Excludes junk files (.DS_Store, __pycache__, .git, etc.).
- Calculates SHA256 hash and size metrics.
"""

from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from pathlib import Path

# Import the validator from the same scripts directory
from validate import validate

EXCLUDED_FILENAMES = {".DS_Store", "Thumbs.db"}
EXCLUDED_DIRNAMES = {"__pycache__", ".git", ".pytest_cache"}
EXCLUDED_EXTENSIONS = {".pyc", ".pyo"}


def sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def package(src_dir: Path, output_zip: Path, skip_validation: bool = False) -> bool:
    if not skip_validation:
        if not validate(src_dir):
            print("\n[ERROR] Packaging aborted because validation failed.")
            return False

    print(f"\nPackaging '{src_dir}' into '{output_zip}'...")
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    if output_zip.exists():
        output_zip.unlink()

    total_files = 0
    total_uncompressed_bytes = 0

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            # Prune excluded directories in-place
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRNAMES]

            for file in files:
                if file in EXCLUDED_FILENAMES:
                    continue
                ext = Path(file).suffix.lower()
                if ext in EXCLUDED_EXTENSIONS:
                    continue

                full_path = Path(root) / file
                rel_path = full_path.relative_to(src_dir)

                # Skip .gitkeep files to keep zip minimal unless directory is empty
                if file == ".gitkeep":
                    continue

                # Add file directly relative to src_dir
                zf.write(full_path, arcname=str(rel_path).replace("\\", "/"))
                total_files += 1
                total_uncompressed_bytes += full_path.stat().st_size

    compressed_bytes = output_zip.stat().st_size
    checksum = sha256_file(output_zip)

    print("\n=== Submission Package Created Successfully ===")
    print(f"Destination:          {output_zip.resolve()}")
    print(f"Total files packed:   {total_files}")
    print(f"Uncompressed size:    {total_uncompressed_bytes / (1024*1024):.2f} MB ({total_uncompressed_bytes:,} bytes)")
    print(f"Compressed zip size:  {compressed_bytes / (1024*1024):.2f} MB ({compressed_bytes:,} bytes)")
    print(f"SHA256 Checksum:      {checksum}")
    print("\nVerification: 'agent.yaml' is located at root of submission.zip:")
    with zipfile.ZipFile(output_zip, "r") as zf:
        namelist = zf.namelist()
        if "agent.yaml" in namelist or "agent.yml" in namelist:
            print("  -> OK: Found root agent config in archive root.")
        else:
            print("  -> ERROR: Missing root agent config in archive root!")
            return False

    return True


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent.parent
    src_path = repo_root / "src"
    dist_zip = repo_root / "dist" / "submission.zip"

    skip_val = "--no-validate" in sys.argv
    success = package(src_path, dist_zip, skip_validation=skip_val)
    sys.exit(0 if success else 1)
