from pathlib import Path

import pytest

from scripts.practice_pack.import_openclaw_pack import ProjectionError, project_skill


SKILL = """---
name: bateson-relationship-pattern-map
description: Relationship pattern mapping through Bateson distinctions.
---

# Relationship Pattern Map

Body.
"""


def test_project_skill_copies_skill_and_support_files(tmp_path: Path):
    source = tmp_path / "openclaw" / "bateson-relationship-pattern-map"
    (source / "references" / "pack").mkdir(parents=True)
    (source / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (source / "references" / "pack" / "PACK.yaml").write_text("id: bateson.relationship-pattern-map\n", encoding="utf-8")

    target = tmp_path / "hermes" / "cognitive"
    result = project_skill(source, target)

    assert result["name"] == "bateson-relationship-pattern-map"
    projected = target / "bateson-relationship-pattern-map"
    assert (projected / "SKILL.md").read_text(encoding="utf-8") == SKILL
    assert (projected / "references" / "pack" / "PACK.yaml").read_text(encoding="utf-8") == "id: bateson.relationship-pattern-map\n"


def test_project_skill_rejects_proposal_frontmatter(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        """---
name: bateson-relationship-pattern-map
description: draft
status: proposal
---

# Draft
""",
        encoding="utf-8",
    )

    with pytest.raises(ProjectionError, match="proposal-only"):
        project_skill(source, tmp_path / "target")
