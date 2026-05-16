import ast
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_FILES = (
    ROOT / "tools" / "read_utf8.py",
    ROOT / "tests" / "test_read_utf8_cli.py",
)


def lint() -> int:
    for path in PYTHON_FILES:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print("lint: syntax ok")
    return 0


def typecheck() -> int:
    print("typecheck: no static type checker configured for M01")
    return 0


def test() -> int:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_read_utf8_cli"],
        cwd=ROOT,
        env=env,
    )
    return result.returncode


def all_checks() -> int:
    for check in (lint, typecheck, test):
        exit_code = check()
        if exit_code != 0:
            return exit_code
    return 0


def main(argv: list[str]) -> int:
    commands = {
        "lint": lint,
        "typecheck": typecheck,
        "test": test,
        "all": all_checks,
    }
    if len(argv) != 2 or argv[1] not in commands:
        print(
            "usage: scripts/validate_current_slice.py <lint|typecheck|test|all>",
            file=sys.stderr,
        )
        return 2
    return commands[argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
