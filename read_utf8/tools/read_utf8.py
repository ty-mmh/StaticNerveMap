import argparse
import sys
from pathlib import Path


MOJIBAKE_MARKERS = (
    "縺",
    "繧",
    "譁",
    "荳",
    "螟",
    "謾",
)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a UTF-8 file with line-numbered Codex-friendly output."
    )
    parser.add_argument("path", help="file to read as UTF-8")
    parser.add_argument("--start", type=positive_int, help="first line to print, inclusive")
    parser.add_argument("--end", type=positive_int, help="last line to print, inclusive")
    parser.add_argument(
        "--repr",
        action="store_true",
        help="print Python repr() for each selected line",
    )
    parser.add_argument(
        "--detect-mojibake",
        action="store_true",
        help="exit non-zero if common mojibake markers are found",
    )
    args = parser.parse_args(argv)

    if args.start is not None and args.end is not None and args.start > args.end:
        parser.error("--start must be less than or equal to --end")

    return args


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise RuntimeError(f"file not found: {path}") from None
    except IsADirectoryError:
        raise RuntimeError(f"path is a directory, not a file: {path}") from None
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"failed to decode as UTF-8: {path}: {exc}") from None
    except OSError as exc:
        raise RuntimeError(f"failed to read file: {path}: {exc}") from None


def selected_lines(lines: list[str], start: int | None, end: int | None) -> list[tuple[int, str]]:
    start_line = start if start is not None else 1
    end_line = end if end is not None else len(lines)
    return [
        (line_no, line)
        for line_no, line in enumerate(lines, start=1)
        if start_line <= line_no <= end_line
    ]


def print_lines(lines: list[tuple[int, str]], repr_mode: bool) -> None:
    for line_no, line in lines:
        content = repr(line) if repr_mode else line
        print(f"{line_no}: {content}")


def find_mojibake(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for line_no, line in lines:
        for marker in MOJIBAKE_MARKERS:
            if marker in line:
                findings.append((line_no, marker))
    return findings


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        lines = read_lines(Path(args.path))
    except RuntimeError as exc:
        print(f"read-utf8: {exc}", file=sys.stderr)
        return 1

    selected = selected_lines(lines, args.start, args.end)
    print_lines(selected, args.repr)

    if args.detect_mojibake:
        findings = find_mojibake(selected)
        for line_no, marker in findings:
            print(
                f"read-utf8: suspicious mojibake marker {marker!r} on line {line_no}",
                file=sys.stderr,
            )
        if findings:
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
