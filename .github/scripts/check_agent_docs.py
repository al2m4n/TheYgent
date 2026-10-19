#!/usr/bin/env python3
"""Fail if the AGENTS.md context files drift out of step with the tree they describe.

Coding agents read each directory's AGENTS.md instead of the code, so a map that names a file
that no longer exists, or omits one that does, sends the next agent the wrong way. This checks
what can be checked mechanically (never whether the prose is still true):

  * every AGENTS.md has a sibling CLAUDE.md containing exactly ``@AGENTS.md``;
  * every AGENTS.md other than the root's is linked from the root AGENTS.md;
  * every relative Markdown link in an AGENTS.md resolves, including ``#anchors`` into .md files;
  * every directory an AGENTS.md declares it covers is fully mapped: each file and subdirectory
    in it (to the declared depth) is named in that AGENTS.md.

A covered directory is declared inside the AGENTS.md with an HTML comment, invisible when
rendered, path relative to the AGENTS.md's own directory:

    <!-- agents-map: covers=src/theygent_control_plane -->
    <!-- agents-map: covers=. depth=2 skip=ui -->

``depth`` (default 1) is how many levels below the directory are checked; ``skip`` is a
comma-separated list of paths (relative to the covered directory) left out. An entry counts as
named when its file name appears in the AGENTS.md, or its name without the extension appears as
a whole word. ``__init__.py``, ``AGENTS.md``, ``CLAUDE.md`` and dotfiles are never required.

Files are read from git: tracked files plus untracked ones that are not ignored.

Usage: check_agent_docs.py [repo-root]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

STUB = "@AGENTS.md\n"
NEVER_REQUIRED = {"__init__.py", "AGENTS.md", "CLAUDE.md", "py.typed"}

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
MARKER_RE = re.compile(r"<!--\s*agents-map:\s*(.*?)\s*-->")
FENCE_RE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def repo_files(root: Path) -> list[PurePosixPath]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode()
    # --cached still lists a tracked file deleted from the working tree; the tree on disk wins.
    return sorted({PurePosixPath(p) for p in out.split("\0") if p and (root / p).exists()})


def github_slug(heading: str) -> str:
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # a linked heading keeps its text
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    body = FENCE_RE.sub("", path.read_text(encoding="utf-8"))
    for heading in HEADING_RE.findall(body):
        slug = github_slug(heading)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def is_named(name: str, text: str) -> bool:
    if name in text:
        return True
    stem = name.split(".", 1)[0] if not name.startswith(".") else name
    return bool(stem) and re.search(rf"(?<![\w-]){re.escape(stem)}(?![\w-])", text) is not None


def markers_in(text: str) -> list[str]:
    # A marker inside a fenced code block is an example, not a declaration.
    return MARKER_RE.findall(FENCE_RE.sub("", text))


def parse_marker(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in raw.split():
        key, sep, value = part.partition("=")
        if not sep or key not in {"covers", "depth", "skip"}:
            raise ValueError(f"unknown agents-map field {part!r} (expected covers=, depth=, skip=)")
        fields[key] = value
    if "covers" not in fields:
        raise ValueError("agents-map marker without covers=")
    return fields


def coverage_problems(
    root: Path, doc: PurePosixPath, text: str, files: list[PurePosixPath]
) -> list[str]:
    problems: list[str] = []
    for raw in markers_in(text):
        try:
            marker = parse_marker(raw)
            depth = int(marker.get("depth", "1"))
        except ValueError as exc:
            problems.append(f"{doc}: {exc}")
            continue
        try:
            covered = PurePosixPath(
                Path(root / doc.parent / marker["covers"]).resolve().relative_to(root).as_posix()
            )
        except ValueError:
            problems.append(f"{doc}: covers={marker['covers']} points outside the repository")
            continue
        if not (root / covered).is_dir():
            problems.append(f"{doc}: covers={marker['covers']} is not a directory")
            continue
        skips = {covered / s.strip() for s in marker.get("skip", "").split(",") if s.strip()}
        entries: set[PurePosixPath] = set()
        for f in files:
            if covered != PurePosixPath(".") and covered not in f.parents:
                continue
            rel_parts = f.relative_to(covered).parts
            # Every file up to `depth` levels down, plus every directory on the way to one.
            for i in range(min(len(rel_parts), depth)):
                entries.add(covered.joinpath(*rel_parts[: i + 1]))
        for entry in sorted(entries):
            if any(entry == s or s in entry.parents for s in skips):
                continue
            # Hidden or cache entries inside the covered directory are never required; the covered
            # directory itself may sit under a dot directory (.github).
            if any(
                part.startswith(".") or part == "__pycache__"
                for part in entry.relative_to(covered).parts
            ):
                continue
            if entry.name in NEVER_REQUIRED or is_named(entry.name, text):
                continue
            kind = "directory" if (root / entry).is_dir() else "file"
            problems.append(
                f"{doc}: {kind} {entry} is not in the map — add a line saying what it does"
            )
    return problems


def link_problems(root: Path, doc: PurePosixPath, text: str) -> list[str]:
    problems: list[str] = []
    for target in LINK_RE.findall(FENCE_RE.sub("", text)):
        if SCHEME_RE.match(target):
            continue
        path_part, _, anchor = target.partition("#")
        dest = (root / doc.parent / path_part).resolve() if path_part else root / doc
        if not dest.exists():
            problems.append(f"{doc}: broken link -> {target}")
        elif anchor and dest.suffix == ".md" and anchor not in anchors_of(dest):
            problems.append(f"{doc}: link to a missing heading -> {target}")
    return problems


def main() -> int:
    if len(sys.argv) > 2:
        print("usage: check_agent_docs.py [repo-root]", file=sys.stderr)
        return 2
    root = Path(sys.argv[1] if len(sys.argv) == 2 else ".").resolve()
    files = repo_files(root)
    docs = [f for f in files if f.name == "AGENTS.md"]
    root_doc = PurePosixPath("AGENTS.md")
    if root_doc not in docs:
        print(f"FAIL: {root / 'AGENTS.md'} is missing.", file=sys.stderr)
        return 1
    root_text = (root / root_doc).read_text(encoding="utf-8")

    problems: list[str] = []
    for doc in docs:
        text = (root / doc).read_text(encoding="utf-8")
        stub = root / doc.parent / "CLAUDE.md"
        if not stub.is_file() or stub.read_text(encoding="utf-8") != STUB:
            problems.append(
                f"{doc.parent / 'CLAUDE.md'}: must exist and contain exactly '@AGENTS.md'"
            )
        if doc != root_doc and f"]({doc})" not in root_text:
            problems.append(f"{doc}: not linked from the root AGENTS.md repository map")
        problems += link_problems(root, doc, text)
        problems += coverage_problems(root, doc, text, files)

    if problems:
        print("FAIL: the AGENTS.md context files are out of step with the tree:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("See 'Keeping the context files current' in the root AGENTS.md.", file=sys.stderr)
        return 1

    covered = sum(len(markers_in((root / d).read_text(encoding="utf-8"))) for d in docs)
    print(f"OK: {len(docs)} AGENTS.md file(s), {covered} mapped director(ies), all in step.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
