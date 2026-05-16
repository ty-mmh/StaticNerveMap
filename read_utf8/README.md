# read-utf8

`read-utf8` is a small helper for inspecting UTF-8 files from Codex on Windows.
It reads files with an explicit UTF-8 decoder and prints predictable,
line-numbered output.

This is useful when a Japanese file is valid UTF-8, but PowerShell or the
console output path makes Codex observe mojibake.

## Quick Start

```powershell
python tools/read_utf8.py docs/README.md
```

Default output includes one-based line numbers:

```text
1: # read-utf8
2:
3: `read-utf8` is a small helper...
```

## Read a Line Range

Use `--start` and `--end` to print an inclusive range:

```powershell
python tools/read_utf8.py docs/README.md --start 10 --end 20
```

Both values are one-based line numbers. If omitted, `--start` defaults to the
first line and `--end` defaults to the last line.

## Inspect Exact Text

Use `--repr` when you need to distinguish real file content from display-path
mojibake:

```powershell
python tools/read_utf8.py docs/README.md --repr --start 10 --end 12
```

This prints Python `repr()` output for each selected line.

## Detect Mojibake Markers

Use `--detect-mojibake` to scan selected lines for common suspicious markers:

```powershell
python tools/read_utf8.py docs/README.md --detect-mojibake
```

The current marker set is:

```text
U+7E3A, U+7E67, U+8B41, U+8373, U+879F, U+8B3E
```

When a marker is found, the tool reports the marker and line number to stderr
and exits non-zero.

Range selection also applies to detection:

```powershell
python tools/read_utf8.py docs/README.md --start 40 --end 80 --detect-mojibake
```

## Exit Codes

- `0`: file was read successfully and no suspicious marker was found.
- `1`: the file could not be read as requested.
- `2`: `--detect-mojibake` found suspicious text.

## Validation

Run the current repository validation with:

```powershell
python scripts/validate_current_slice.py all
```

In environments where `bash` is available, the wrapper command is:

```powershell
bash scripts/validate_current_slice.sh all
```

The Windows Codex harness used for this repository may block WSL-backed `bash`,
so the Python command is the reliable local validation entry point here.

## Non-Goals

`read-utf8` does not rewrite files, guess encodings, replace editors, or perform
general linting. It is only a small observation tool for safer UTF-8 inspection.
