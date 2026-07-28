"""Packet model validation and serialization tests."""

from typing import Any

import pytest

from attention_manager.packet import (
    MAX_CONTEXT_CHARS,
    OnTimeout,
    Option,
    Packet,
    Recommendation,
    Resolution,
    Source,
    Urgency,
    new_packet_id,
)


def make_decision_packet(**overrides) -> Packet:
    kwargs: dict[str, Any] = dict(
        question="Migrate now, or keep the shim?",
        options=[
            Option(id="A", label="Migrate now", consequence="breaks two repos"),
            Option(id="B", label="Keep shim"),
        ],
        source=Source(kind="decision", session_id="sess-1"),
    )
    kwargs.update(overrides)
    return Packet(**kwargs)


def make_permission_packet(**overrides) -> Packet:
    kwargs: dict[str, Any] = dict(
        question="Allow or deny: run rm -rf build/?",
        options=[Option(id="allow", label="Allow"), Option(id="deny", label="Deny")],
        source=Source(kind="permission"),
    )
    kwargs.update(overrides)
    return Packet(**kwargs)


class TestValidation:
    def test_good_decision_packet_validates(self):
        make_decision_packet().validate()

    def test_good_permission_packet_validates(self):
        make_permission_packet().validate()

    def test_id_format(self):
        pid = new_packet_id()
        assert pid.startswith("pkt-")
        # pkt-yyyymmdd-HHMMSS-xxxx
        parts = pid.split("-")
        assert len(parts) == 4
        assert len(parts[1]) == 8 and len(parts[2]) == 6 and len(parts[3]) == 4

    def test_empty_question_rejected(self):
        with pytest.raises(ValueError, match="question"):
            make_decision_packet(question="  ").validate()

    def test_bad_kind_rejected(self):
        with pytest.raises(ValueError, match="source.kind"):
            make_decision_packet(source=Source(kind="banana")).validate()

    def test_empty_options_rejected(self):
        with pytest.raises(ValueError, match="options"):
            make_decision_packet(options=[]).validate()

    def test_duplicate_option_ids_rejected(self):
        with pytest.raises(ValueError, match="unique"):
            make_decision_packet(options=[Option(id="A", label="x"), Option(id="A", label="y")]).validate()

    def test_permission_requires_exactly_allow_deny(self):
        with pytest.raises(ValueError, match="permission packets require exactly"):
            make_permission_packet(options=[Option(id="A", label="x"), Option(id="B", label="y")]).validate()
        with pytest.raises(ValueError, match="permission packets require exactly"):
            make_permission_packet(
                options=[
                    Option(id="allow", label="Allow"),
                    Option(id="deny", label="Deny"),
                    Option(id="maybe", label="Maybe"),
                ]
            ).validate()

    def test_context_over_8000_chars_rejected(self):
        with pytest.raises(ValueError, match="8000"):
            make_decision_packet(context="x" * (MAX_CONTEXT_CHARS + 1)).validate()

    def test_context_at_8000_chars_ok(self):
        make_decision_packet(context="x" * MAX_CONTEXT_CHARS).validate()

    def test_recommendation_option_must_exist(self):
        with pytest.raises(ValueError, match="recommendation.option"):
            make_decision_packet(recommendation=Recommendation(option="Z")).validate()

    def test_bad_tier_rejected(self):
        with pytest.raises(ValueError, match="urgency.tier"):
            make_decision_packet(urgency=Urgency(tier="whenever")).validate()

    def test_on_timeout_apply_option_requires_option(self):
        with pytest.raises(ValueError, match="apply-option"):
            make_decision_packet(
                urgency=Urgency(deadline="2026-07-27T00:00:00Z", on_timeout=OnTimeout(action="apply-option"))
            ).validate()

    def test_on_timeout_option_must_exist(self):
        with pytest.raises(ValueError, match="on_timeout.option"):
            make_decision_packet(
                urgency=Urgency(
                    deadline="2026-07-27T00:00:00Z", on_timeout=OnTimeout(action="apply-option", option="Z")
                )
            ).validate()

    def test_bad_on_timeout_action_rejected(self):
        with pytest.raises(ValueError, match="on_timeout.action"):
            make_decision_packet(
                urgency=Urgency(deadline="2026-07-27T00:00:00Z", on_timeout=OnTimeout(action="shrug"))
            ).validate()

    def test_resolution_answer_must_be_an_option(self):
        with pytest.raises(ValueError, match="resolution.answer"):
            make_decision_packet(
                resolution=Resolution(answer="Z", answered_by="human", answered_at="2026-07-26T17:00:00Z")
            ).validate()


class TestSerialization:
    def test_json_round_trip(self):
        packet = make_decision_packet(
            recommendation=Recommendation(option="B", rationale="safer", confidence="medium"),
            context="minimal facts",
            links={"resume": "amplifier session resume sess-1", "files": ["a.py"]},
            urgency=Urgency(
                tier="today", deadline="2026-07-27T00:00:00Z", on_timeout=OnTimeout(action="apply-option", option="B")
            ),
        )
        packet.validate()
        restored = Packet.from_json(packet.to_json())
        restored.validate()
        assert restored.to_dict() == packet.to_dict()

    def test_from_json_rejects_garbage(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            Packet.from_json("{nope")

    def test_from_json_rejects_missing_fields(self):
        with pytest.raises(ValueError, match="missing required field"):
            Packet.from_json('{"id": "pkt-x"}')

    def test_none_fields_omitted_from_json(self):
        d = make_decision_packet().to_dict()
        assert "recommendation" not in d
        assert "resolution" not in d
        assert "triage" not in d
        assert "consequence" not in d["options"][1]
