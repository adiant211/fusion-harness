TOOL = {
    "name": "find_text",
    "description": (
        "Search workspace files for a regular expression. Returns `path:line: text` for each "
        "match (at most 200), skipping .git, virtualenvs, node_modules and binary files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "A Python regular expression."},
            "path": {"type": "string", "description": "File or directory to search (default '.')."},
            "glob": {"type": "string", "description": "Only file names matching this glob, e.g. '*.py'."},
        },
        "required": ["pattern"],
    },
}

import fnmatch
import json
import os
import re
import sys

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".fusion"}
LIMIT = 200


def files(root, glob):
    if os.path.isfile(root):
        yield root
        return
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        for n in sorted(names):
            if fnmatch.fnmatch(n, glob):
                yield os.path.join(d, n)


def main():
    args = json.load(sys.stdin)
    rx = re.compile(args["pattern"])
    hits = []
    for path in files(args.get("path") or ".", args.get("glob") or "*"):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if b"\0" in raw[:1024]:
            continue
        for i, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{os.path.relpath(path)}:{i}: {line.strip()[:200]}")
                if len(hits) >= LIMIT:
                    print("\n".join(hits))
                    print(f"[stopped at {LIMIT} matches; narrow the pattern, path or glob]")
                    return
    print("\n".join(hits) if hits else "no matches")


if __name__ == "__main__":
    main()
