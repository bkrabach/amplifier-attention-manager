"""Rulebook unit tests — template, sections, token cap, proposals lifecycle."""

import pytest

from attention_manager.rulebook import DEFAULT_TOKEN_CAP
from attention_manager.rulebook import SECTIONS
from attention_manager.rulebook import Rulebook
from attention_manager.rulebook import approx_tokens


@pytest.fixture
def rulebook(tmp_path) -> Rulebook:
    return Rulebook(home=tmp_path)


class TestTemplate:
    def test_created_on_first_read(self, rulebook, tmp_path):
        assert not rulebook.path.exists()
        content, tokens = rulebook.read()
        assert rulebook.path == tmp_path / "rulebook.md"
        assert rulebook.path.exists()
        assert tokens == approx_tokens(content)

    def test_template_has_all_five_design_sections_in_order(self, rulebook):
        content, _ = rulebook.read()
        positions = [content.find(f"## {s}") for s in SECTIONS]
        assert all(p >= 0 for p in positions), f"missing sections: {[s for s, p in zip(SECTIONS, positions) if p < 0]}"
        assert positions == sorted(positions), "sections out of canonical order"

    def test_each_section_has_an_intro_line(self, rulebook):
        content, _ = rulebook.read()
        for section in SECTIONS:
            after = content.split(f"## {section}", 1)[1]
            intro = after.strip().splitlines()[0]
            assert intro.startswith("_"), f"section {section!r} missing intro comment"

    def test_existing_file_not_overwritten(self, rulebook):
        rulebook.read()
        rulebook.append_rule("Edge cases", "custom rule survives re-read")
        content, _ = rulebook.read()
        assert "custom rule survives re-read" in content

    def test_token_count_is_len_div_4(self):
        assert approx_tokens("x" * 400) == 100


class TestAppendRule:
    def test_appends_bullet_to_correct_section(self, rulebook):
        rulebook.append_rule("Auto-answer rules", "Prefer compat shims when downstream owners are unavailable.")
        content, _ = rulebook.read()
        section_body = content.split("## Auto-answer rules", 1)[1].split("## ", 1)[0]
        assert "- Prefer compat shims when downstream owners are unavailable." in section_body
        # ...and NOT in any other section
        before = content.split("## Auto-answer rules", 1)[0]
        assert "Prefer compat shims" not in before

    def test_appends_to_last_section_works(self, rulebook):
        rulebook.append_rule("When you cannot proceed", "Bounce, never guess.")
        content, _ = rulebook.read()
        assert (
            content.rstrip().splitlines()[-2:-1] == ["- Bounce, never guess."]
            or "- Bounce, never guess." in (content.split("## When you cannot proceed", 1)[1])
        )

    def test_multiple_appends_accumulate_in_order(self, rulebook):
        rulebook.append_rule("Edge cases", "first rule")
        rulebook.append_rule("Edge cases", "second rule")
        content, _ = rulebook.read()
        section = content.split("## Edge cases", 1)[1].split("## ", 1)[0]
        assert section.find("- first rule") < section.find("- second rule")

    def test_unknown_section_refused(self, rulebook):
        with pytest.raises(ValueError, match="unknown rulebook section"):
            rulebook.append_rule("Made Up Section", "nope")

    def test_empty_sentence_refused(self, rulebook):
        with pytest.raises(ValueError, match="non-empty"):
            rulebook.append_rule("Edge cases", "   ")

    def test_cap_refusal_is_loud_and_instructive(self, tmp_path):
        small = Rulebook(home=tmp_path, token_cap=10)  # template alone exceeds this
        with pytest.raises(ValueError, match="consolidate"):
            small.append_rule("Edge cases", "one more rule")
        content, _ = small.read()
        assert "one more rule" not in content  # refused append changed nothing

    def test_default_cap_is_2000(self, rulebook):
        assert rulebook.token_cap == DEFAULT_TOKEN_CAP == 2000


class TestProposals:
    def test_append_and_list(self, rulebook):
        record = rulebook.append_proposal("pkt-x", "Edge cases", "a rule", "because")
        assert record["status"] == "proposed"
        assert record["id"].startswith("rp-")
        listed = rulebook.list_proposals()
        assert len(listed) == 1
        assert listed[0]["packet_id"] == "pkt-x"

    def test_double_propose_refused(self, rulebook):
        rulebook.append_proposal("pkt-x", "Edge cases", "a rule", "because")
        with pytest.raises(ValueError, match="never double-propose"):
            rulebook.append_proposal("pkt-x", "Edge cases", "another", "because")
        with pytest.raises(ValueError, match="never double-propose"):
            rulebook.record_none("pkt-x", "one-off")

    def test_record_none_counts_for_idempotency(self, rulebook):
        rulebook.record_none("pkt-y", "genuinely one-off")
        assert "pkt-y" in rulebook.proposal_packet_ids()
        with pytest.raises(ValueError, match="never double-propose"):
            rulebook.append_proposal("pkt-y", "Edge cases", "late rule", "because")

    def test_unknown_section_in_proposal_refused(self, rulebook):
        with pytest.raises(ValueError, match="unknown rulebook section"):
            rulebook.append_proposal("pkt-x", "Nope", "a rule", "because")

    def test_apply_appends_to_correct_section_and_marks_applied(self, rulebook):
        record = rulebook.append_proposal("pkt-x", "Escalation thresholds", "Escalate on third retry.", "why")
        applied = rulebook.apply(record["id"])
        assert applied["status"] == "applied"
        assert "applied_at" in applied
        content, _ = rulebook.read()
        section = content.split("## Escalation thresholds", 1)[1].split("## ", 1)[0]
        assert "- Escalate on third retry." in section
        assert rulebook.list_proposals()[0]["status"] == "applied"

    def test_apply_refuses_non_proposed(self, rulebook):
        record = rulebook.append_proposal("pkt-x", "Edge cases", "a rule", "why")
        rulebook.apply(record["id"])
        with pytest.raises(ValueError, match="only 'proposed'"):
            rulebook.apply(record["id"])

    def test_apply_over_cap_leaves_proposal_proposed(self, tmp_path):
        small = Rulebook(home=tmp_path, token_cap=10)
        record = small.append_proposal("pkt-x", "Edge cases", "a rule", "why")
        with pytest.raises(ValueError, match="consolidate"):
            small.apply(record["id"])
        assert small.get_proposal(record["id"])["status"] == "proposed"  # nothing half-applied

    def test_apply_unknown_id_raises(self, rulebook):
        with pytest.raises(KeyError):
            rulebook.apply("rp-nope")

    def test_reject_requires_reason(self, rulebook):
        record = rulebook.append_proposal("pkt-x", "Edge cases", "a rule", "why")
        with pytest.raises(ValueError, match="requires a reason"):
            rulebook.reject(record["id"], "  ")

    def test_reject_marks_rejected_with_reason(self, rulebook):
        record = rulebook.append_proposal("pkt-x", "Edge cases", "a rule", "why")
        rejected = rulebook.reject(record["id"], "too specific")
        assert rejected["status"] == "rejected"
        assert rejected["reject_reason"] == "too specific"
        content, _ = rulebook.read()
        assert "- a rule" not in content  # never touched the rulebook
