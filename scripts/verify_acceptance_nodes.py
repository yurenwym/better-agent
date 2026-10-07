"""Verify that every pytest node referenced in callsite-inventory.md section 7
actually exists in the collected test set.

Read-only: collects test ids, deletes nothing.
Usage: D:/pycharm/python.exe scripts/verify_acceptance_nodes.py
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path("D:/RAG/better/backend")
INV = pathlib.Path(
    "D:/RAG/better/docs/acceptance/context-snapshot-phase2/callsite-inventory.md"
)

text = INV.read_text(encoding="utf-8")
# Section 7 is the last top-level section; 7.1 is a sub-section of it. Take
# everything from the "## 7." heading to the end so both are scanned. (Do not
# use text.split("## 7."): the "### 7.1" heading also contains that substring,
# which would silently cut the scan off at 7.1.)
_start = re.search(r"^## 7\.", text, re.M)
assert _start is not None, "section 7 heading not found in callsite-inventory.md"
sec = text[_start.start():]

# Section 7 rows mix absolute refs ("tests/x.py::node") with relative ones
# ("::node"), so remember the last file seen while scanning left to right.
refs: list[str] = []
current_file = None
for cell in re.findall(r"`([^`]+)`", sec):
    for piece in cell.split("、"):
        piece = piece.strip()
        if piece.startswith("tests/") and "::" in piece:
            current_file = piece.split("::")[0]
            refs.append(piece)
        elif piece.startswith("::") and current_file:
            refs.append(current_file + piece)

refs = list(dict.fromkeys(refs))
print(f"resolved node patterns in section 7: {len(refs)}")

files = sorted({r.split("::")[0] for r in refs})
print(f"distinct test files: {len(files)}")
for f in files:
    print("   ", f)

out = subprocess.run(
    [
        "D:/pycharm/python.exe",
        "-m",
        "pytest",
        *files,
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp=C:/tmp/verify-nodes",
    ],
    cwd=ROOT,
    capture_output=True,
    text=True,
)

collected = {
    line.strip()
    for line in out.stdout.splitlines()
    if line.strip().startswith("tests/") and "::" in line
}
print(f"collected node ids: {len(collected)}")
if not collected:
    print("--- stdout tail ---")
    print(out.stdout[-3000:])
    print("--- stderr tail ---")
    print(out.stderr[-3000:])


def matches(ref: str) -> bool:
    """A reference resolves if pytest would accept it as a selector.

    pytest accepts the bare base name of a parameterised test (it then selects
    every parameter), so `...::test_g10_x` is a valid reference even though the
    collected ids are `...::test_g10_x[openai_compatible]` and friends.
    """
    f, node = ref.split("::")
    if node.endswith("*"):
        prefix = f + "::" + node[:-1]
        return any(c.startswith(prefix) for c in collected)
    full = f + "::" + node
    return full in collected or any(c.startswith(full + "[") for c in collected)


bad = [r for r in refs if not matches(r)]
print(f"UNRESOLVED: {len(bad)}")
for r in bad:
    print("   X", r)

# Reverse direction: collected nodes that section 7 never mentions.
prefixes = []
for ref in refs:
    f, node = ref.split("::")
    prefixes.append(f + "::" + (node[:-1] if node.endswith("*") else node))
unmapped = [
    c
    for c in sorted(collected)
    if not any(c.startswith(p) for p in prefixes)
]
print(f"collected-but-unmapped: {len(unmapped)}")
for c in unmapped:
    print("   ?", c)
