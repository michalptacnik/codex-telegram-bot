#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from codex_telegram_bot.plugins.manifest import load_manifest, validate_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate plugin manifest JSON")
    parser.add_argument("manifest_path", help="Path to plugin manifest JSON file")
    args = parser.parse_args()

    try:
        manifest = load_manifest(Path(args.manifest_path))
    except Exception as exc:
        print(f"Invalid manifest JSON: {exc}", file=sys.stderr)
        return 1

    errors = validate_manifest(manifest)
    if errors:
        print("Manifest validation failed:")
        for err in errors:
            print(f"- {err}")
        return 2
    print("Manifest validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

