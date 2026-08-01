"""Owner-side Amplifier surface — structural checks on the shipped files.

Guards the composition contract (validated with the foundation expert):
- the skill is a spec-conformant SKILL.md (YAML frontmatter with name + description);
- the behavior ships the skill via tool-skills config.skills using the FULL git
  URL (a top-level ``skills:`` bundle key would be silently ignored) and
  includes the awareness context;
- the root bundle.md frontmatter carries the right bundle name (distinct from
  bundles/test-worker.md's ``attention-test-worker``).
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

SKILL_PATH = REPO / "skills" / "attention-manager" / "SKILL.md"
BEHAVIOR_PATH = REPO / "behaviors" / "attention-manager.yaml"
BUNDLE_PATH = REPO / "bundle.md"
AWARENESS_INCLUDE = "attention-manager:context/attention-manager-awareness.md"
SKILLS_GIT_URL = "git+https://github.com/bkrabach/amplifier-attention-manager@main#subdirectory=skills"


def _frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter between the leading --- fences."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} must start with a --- frontmatter fence"
    _, fm, _body = text.split("---", 2)
    data = yaml.safe_load(fm)
    assert isinstance(data, dict), f"{path.name} frontmatter must be a YAML mapping"
    return data


def test_skill_frontmatter_parses_with_name_and_description():
    fm = _frontmatter(SKILL_PATH)
    assert fm["name"] == "attention-manager"
    assert isinstance(fm["description"], str) and fm["description"].strip()


def test_behavior_yaml_ships_skill_via_tool_skills_and_includes_awareness():
    data = yaml.safe_load(BEHAVIOR_PATH.read_text(encoding="utf-8"))
    assert data["bundle"]["name"] == "attention-manager-behavior"
    # A top-level `skills:` key is silently ignored by the loader — the skill
    # MUST travel via the tool-skills module config instead.
    assert "skills" not in data
    tools = data["tools"]
    (skills_tool,) = [t for t in tools if t["module"] == "tool-skills"]
    assert SKILLS_GIT_URL in skills_tool["config"]["skills"]
    assert AWARENESS_INCLUDE in data["context"]["include"]


def test_root_bundle_frontmatter_has_correct_name():
    fm = _frontmatter(BUNDLE_PATH)
    assert fm["bundle"]["name"] == "attention-manager"
    # Must stay distinct from the worker bundle's name.
    worker_fm = _frontmatter(REPO / "bundles" / "test-worker.md")
    assert worker_fm["bundle"]["name"] != fm["bundle"]["name"]


def test_awareness_context_file_exists_and_points_at_skill():
    text = (REPO / "context" / "attention-manager-awareness.md").read_text(encoding="utf-8")
    assert 'load_skill(skill_name="attention-manager")' in text
