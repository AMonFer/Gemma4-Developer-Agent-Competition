#!/usr/bin/env python3
"""Submission script for Kaggle using the official Kaggle CLI API.

Usage:
    python scripts/submit.py -m "Experiment description"
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

COMPETITION_SLUG = "gemma-4-developer-agent"


def submit(message: str | None, skip_validation: bool = False) -> bool:
    repo_root = Path(__file__).resolve().parent.parent
    dist_zip = repo_root / "dist" / "submission.zip"
    src_dir = repo_root / "src"

    # Always ensure zip is fresh by repackaging
    from package import package

    print("Step 1: Validating and packaging submission...")
    if not package(src_dir, dist_zip, skip_validation=skip_validation):
        print("\n[ERROR] Packaging failed. Aborting submission.")
        return False

    # Check if kaggle command is available
    kaggle_bin = shutil.which("kaggle")
    if not kaggle_bin:
        print("\n[ERROR] Kaggle CLI is not found in your PATH.")
        print("To install Kaggle CLI:")
        print("    pip install kaggle")
        print("\nTo configure credentials:")
        print("    1. Go to https://www.kaggle.com/settings -> 'Create New Token'.")
        print("    2. Place 'kaggle.json' in ~/.kaggle/ (Linux/Mac) or %USERPROFILE%/.kaggle/ (Windows).")
        return False

    if not message:
        message = input("\nEnter a description for this submission (or press Enter for default): ").strip()
        if not message:
            message = "Automated submission from gemma-agent pipeline"

    print(f"\nStep 2: Submitting to Kaggle competition '{COMPETITION_SLUG}'...")
    print(f"File:    {dist_zip}")
    print(f"Message: {message}")

    cmd = [
        kaggle_bin,
        "competitions",
        "submit",
        "-c",
        COMPETITION_SLUG,
        "-f",
        str(dist_zip),
        "-m",
        message,
    ]

    try:
        res = subprocess.run(cmd, check=True)
        print("\n[SUCCESS] Submission sent to Kaggle!")
        print(f"Check your leaderboard and submission status at:")
        print(f"https://www.kaggle.com/competitions/{COMPETITION_SLUG}/submissions")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] Kaggle CLI returned error code {e.returncode}.")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Submit agent package to Kaggle")
    parser.add_argument("-m", "--message", type=str, help="Submission description message")
    parser.add_argument("--no-validate", action="store_true", help="Skip pre-validation")
    args = parser.parse_args()

    success = submit(args.message, skip_validation=args.no_validate)
    sys.exit(0 if success else 1)
