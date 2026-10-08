"""Read-only environment check; does not install packages or modify global settings."""
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    result = {"python": sys.version.split()[0], "python_ok": sys.version_info >= (3, 11), "packages": {}}
    for name in ("openpyxl", "pypdf"):
        try:
            result["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][name] = None
    try:
        node = os.environ.get("HARNESS_NODE", "node")
        version = subprocess.run([node, "--version"], check=True, capture_output=True, text=True, timeout=30)
        result["node"] = version.stdout.strip()
        result["node_ok"] = int(result["node"].lstrip("v").split(".")[0]) >= 20
        probe = subprocess.run([node, str(Path(__file__).with_name("workbook_io.mjs")), "help", "range.dataValidation"],
                               capture_output=True, text=True, timeout=30)
        result["artifact_tool"] = probe.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        result.update({"node_ok": False, "artifact_tool": False})
    result["ready"] = result["python_ok"] and all(result["packages"].values()) and result["node_ok"] and result["artifact_tool"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
