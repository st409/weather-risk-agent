"""Cross-platform setup, test, eval, seed, and dev tasks.

`make test` and `python scripts/tasks.py test` run the same work.
This file stays compatible with Python 3.7 so an older default `python`
can still bootstrap a 3.10+ virtualenv.
"""

from __future__ import print_function

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def venv_python():
    if os.name == "nt":
        return os.path.join(ROOT, "backend", ".venv", "Scripts", "python.exe")
    return os.path.join(ROOT, "backend", ".venv", "bin", "python")


def _run(args, cwd=None):
    print("+", " ".join(args))
    subprocess.check_call(args, cwd=cwd or ROOT)


def find_base_python():
    if sys.version_info >= (3, 10):
        return sys.executable
    launcher = shutil.which("py")
    if launcher:
        for version in ("3.13", "3.12", "3.11", "3.10"):
            try:
                out = subprocess.check_output(
                    [launcher, "-" + version, "-c", "import sys; print(sys.executable)"],
                    universal_newlines=True,
                )
            except (subprocess.CalledProcessError, OSError):
                continue
            path = out.strip()
            if path:
                return path
    raise SystemExit("Python 3.10 or newer is required to create the virtualenv.")


def setup():
    base = find_base_python()
    venv_dir = os.path.join(ROOT, "backend", ".venv")
    if not os.path.exists(venv_python()):
        _run([base, "-m", "venv", venv_dir])
    py = venv_python()
    _run([py, "-m", "pip", "install", "--upgrade", "pip"])
    _run([py, "-m", "pip", "install", "-e", "backend[dev]"])
    npm = "npm.cmd" if os.name == "nt" else "npm"
    _run([npm, "install"], cwd=os.path.join(ROOT, "frontend"))
    print("Setup complete.")


def _require_venv():
    py = venv_python()
    if not os.path.exists(py):
        raise SystemExit("Virtualenv not found. Run: python scripts/tasks.py setup")
    return py


def seed():
    _run([_require_venv(), "-m", "app.cli", "seed"], cwd=os.path.join(ROOT, "backend"))


def score_once():
    _run([_require_venv(), "-m", "app.cli", "score-once"], cwd=os.path.join(ROOT, "backend"))


def test():
    _run([_require_venv(), "-m", "pytest"], cwd=os.path.join(ROOT, "backend"))


def eval_():
    _run([_require_venv(), "evals/run_eval.py"], cwd=os.path.join(ROOT, "backend"))


def dev():
    _run([_require_venv(), os.path.join(ROOT, "scripts", "dev.py")])


def main():
    commands = {
        "setup": setup,
        "seed": seed,
        "score-once": score_once,
        "test": test,
        "eval": eval_,
        "dev": dev,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print("usage: python scripts/tasks.py setup|dev|test|eval|seed|score-once")
        return 1
    commands[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
