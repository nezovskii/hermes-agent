#!/usr/bin/env python3
"""Project an approved OpenClaw practice pack into a Hermes skill directory.

The script expects an OpenClaw skill/workshop-applied directory containing
`SKILL.md` and optional text support files under standard skill support
folders. It copies the skill into a Hermes skills category and rejects proposal
frontmatter so Hermes only consumes applied/approved content.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ALLOWED_SUPPORT_DIRS = {"assets", "examples", "references", "scripts", "templates"}
FORBIDDEN_FRONTMATTER_FIELDS = {"status", "date"}


class ProjectionError(RuntimeError):
    pass


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ProjectionError("SKILL.md must start with YAML frontmatter")
    try:
        _, fm, _body = text.split("---", 2)
    except ValueError as exc:
        raise ProjectionError("SKILL.md frontmatter is not closed") from exc
    result: dict[str, str] = {}
    for line in fm.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"\'')
    return result


def validate_skill(skill_path: Path) -> tuple[str, str]:
    if not skill_path.exists():
        raise ProjectionError(f"missing SKILL.md: {skill_path}")
    text = skill_path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(text)
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not name:
        raise ProjectionError("SKILL.md frontmatter must include name")
    if not description:
        raise ProjectionError("SKILL.md frontmatter must include description")
    forbidden = sorted(FORBIDDEN_FRONTMATTER_FIELDS.intersection(frontmatter))
    if forbidden:
        raise ProjectionError(f"refusing to project proposal-only frontmatter fields: {', '.join(forbidden)}")
    return name, text


def copy_support_files(source_dir: Path, target_dir: Path) -> list[Path]:
    copied: list[Path] = []
    for child in sorted(source_dir.iterdir()):
        if child.name == "SKILL.md":
            continue
        if child.name.startswith("."):
            continue
        if child.name not in ALLOWED_SUPPORT_DIRS:
            continue
        if not child.is_dir():
            continue
        destination = target_dir / child.name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(child, destination)
        copied.extend(path for path in destination.rglob("*") if path.is_file())
    return copied


def project_skill(source_dir: Path, target_root: Path) -> dict[str, object]:
    source_dir = source_dir.expanduser().resolve()
    target_root = target_root.expanduser().resolve()
    name, skill_text = validate_skill(source_dir / "SKILL.md")
    target_dir = target_root / name
    target_dir.mkdir(parents=True, exist_ok=True)
    skill_path = target_dir / "SKILL.md"
    skill_path.write_text(skill_text, encoding="utf-8")
    support_files = copy_support_files(source_dir, target_dir)
    return {
        "name": name,
        "target_dir": str(target_dir),
        "skill_path": str(skill_path),
        "support_files": [str(path) for path in support_files],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True, help="Applied OpenClaw skill/practice pack directory")
    parser.add_argument("--target", required=True, help="Hermes skills category/root directory")
    args = parser.parse_args(argv)

    try:
        result = project_skill(Path(args.pack), Path(args.target))
    except ProjectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"projected Hermes skill: {result['name']}")
    print(f"target_dir: {result['target_dir']}")
    print(f"skill_path: {result['skill_path']}")
    print(f"support_files: {len(result['support_files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
