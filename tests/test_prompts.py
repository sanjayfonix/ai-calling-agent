"""
Tests for system prompts and tool definitions.
"""

import json

from app.prompts import SYSTEM_PROMPT, TOOL_DEFINITIONS


class TestSystemPrompt:

    def test_prompt_is_not_empty(self):
        assert len(SYSTEM_PROMPT) > 100

    def test_prompt_mentions_consent(self):
        assert "consent" in SYSTEM_PROMPT.lower()

    def test_prompt_mentions_recording(self):
        assert "recorded" in SYSTEM_PROMPT.lower()

    def test_prompt_mentions_all_required_fields(self):
        required = [
            "Full Name",
            "Email",
            "Age",
            "Zip Code",
            "State",
            "Insurance",
            "Doctor",
            "Medications",
        ]
        for field in required:
            assert field.lower() in SYSTEM_PROMPT.lower(), f"Missing: {field}"


class TestToolDefinitions:

    def test_three_tools_defined(self):
        assert len(TOOL_DEFINITIONS) == 3

    def test_tool_names(self):
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert names == {"save_customer_data", "record_consent", "end_call"}

    def test_save_customer_data_has_required_field(self):
        save_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "save_customer_data")
        assert "full_name" in save_tool["parameters"]["required"]

    def test_record_consent_has_consent_given(self):
        consent_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "record_consent")
        assert "consent_given" in consent_tool["parameters"]["properties"]
        assert "consent_given" in consent_tool["parameters"]["required"]

    def test_end_call_has_reason_enum(self):
        end_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "end_call")
        reason_prop = end_tool["parameters"]["properties"]["reason"]
        assert "enum" in reason_prop
        assert "completed" in reason_prop["enum"]
        assert "no_consent" in reason_prop["enum"]

    def test_tools_are_valid_json(self):
        """Tool definitions should serialize to valid JSON."""
        serialized = json.dumps(TOOL_DEFINITIONS)
        parsed = json.loads(serialized)
        assert len(parsed) == 3
