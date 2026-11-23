"""Run the API and the Vite UI together. Ctrl+C stops both."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    return ROOT / "backend" / ".venv" / "bin" / "python"


def main() -> int:
    python = venv_python()
    if not python.exists():
        print("Run setup first. The virtualenv is missing.", file=sys.stderr)
        return 1
    npm = "npm.cmd" if os.name == "nt" else "npm"
    api = subprocess.Popen(
        [str(python), "-m", "uvicorn", "app.main:app", "--reload", "--port", "8000"],
        cwd=str(ROOT / "backend"),
    )
    ui = subprocess.Popen([npm, "run", "dev", "--", "--host", "127.0.0.1"], cwd=str(ROOT / "frontend"))
    print("API  http://127.0.0.1:8000")
    print("UI   http://127.0.0.1:5173")
    try:
        while True:
            if api.poll() is not None or ui.poll() is not None:
                break
            try:
                api.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        for proc in (ui, api):
            if proc.poll() is None:
                proc.terminate()
        for proc in (ui, api):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
