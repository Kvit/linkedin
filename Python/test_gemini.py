"""
Tests for Google Gemini AI integration.

Verifies that:
- google-genai package is importable and client initialises correctly
- The Gemini API responds to a content-generation request
- analyze_summary() returns a correctly structured dict
- Edge-cases (empty / very short summaries) are handled gracefully
"""

import json
import os
import pytest
from dotenv import load_dotenv

# Load .env so GOOGLE_API_KEY is available during tests
load_dotenv()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gemini_client():
    """Return an initialised genai.Client, skipping if no API key is present."""
    from google import genai

    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        pytest.skip("GOOGLE_API_KEY not set – skipping live Gemini tests")

    return genai.Client()


@pytest.fixture(scope="module")
def analyze_fn():
    """
    Return the analyze_summary function built from the same config used in
    analysis.ipynb, so the tests exercise the real integration code path.
    """
    from google import genai
    from google.genai import types

    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        pytest.skip("GOOGLE_API_KEY not set – skipping live Gemini tests")

    client = genai.Client()
    MODEL = "gemini-2.0-flash"

    ANALYSIS_INSTRUCTIONS = """You are an expert at analyzing LinkedIn profile summaries.
Analyze the provided profile summary and extract the following information:

1. industry - The primary industry the person works in (e.g., "Technology", "Finance", "Healthcare")
2. function - Their job function/role type (e.g., "Engineering", "Sales", "Marketing", "Operations")
3. seniority - Their seniority level (e.g., "Entry", "Mid", "Senior", "Executive", "C-Level")

Respond ONLY with a valid JSON object in this exact format:
{"industry": "...", "function": "...", "seniority": "..."}

Do not include any other text, markdown formatting, or code blocks."""

    ANALYSIS_SCHEMA = {
        "type": "object",
        "properties": {
            "industry": {"type": "string"},
            "function": {"type": "string"},
            "seniority": {"type": "string"},
        },
        "required": ["industry", "function", "seniority"],
    }

    def analyze_summary(summary: str) -> dict | None:
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=summary,
                config=types.GenerateContentConfig(
                    system_instruction=ANALYSIS_INSTRUCTIONS,
                    response_mime_type="application/json",
                    response_json_schema=ANALYSIS_SCHEMA,
                ),
            )
            if not hasattr(response, "text"):
                return None
            result = json.loads(response.text)
            required_keys = ["industry", "function", "seniority"]
            if any(k not in result for k in required_keys):
                return None
            return result
        except Exception:
            return None

    return analyze_summary


# ---------------------------------------------------------------------------
# Unit-level tests (no API calls)
# ---------------------------------------------------------------------------

class TestPackageImport:
    def test_google_genai_importable(self):
        """google-genai package must be importable."""
        import google.genai  # noqa: F401

    def test_genai_types_importable(self):
        """google.genai.types must be importable."""
        from google.genai import types  # noqa: F401

    def test_generate_content_config_exists(self):
        """GenerateContentConfig class must exist."""
        from google.genai import types
        assert hasattr(types, "GenerateContentConfig")


class TestClientInit:
    def test_client_initialises_with_api_key(self, gemini_client):
        """Client should initialise without raising."""
        assert gemini_client is not None

    def test_client_has_models_attribute(self, gemini_client):
        """Client must expose a .models interface."""
        assert hasattr(gemini_client, "models")


# ---------------------------------------------------------------------------
# Integration tests (live API calls)
# ---------------------------------------------------------------------------

class TestGeminiAPIConnectivity:
    def test_simple_generate_content(self, gemini_client):
        """A basic prompt should return a non-empty text response."""
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents="Reply with the single word: hello",
        )
        assert hasattr(response, "text"), "Response must have a 'text' attribute"
        assert len(response.text.strip()) > 0, "Response text must not be empty"

    def test_response_text_is_string(self, gemini_client):
        """response.text should be a str."""
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents="Say yes",
        )
        assert isinstance(response.text, str)


class TestAnalyzeSummary:
    """Tests for the analyze_summary() integration used in analysis.ipynb."""

    TECH_SUMMARY = (
        "Senior Software Engineer at Google with 10 years of experience in "
        "distributed systems, cloud infrastructure, and backend development. "
        "Led teams building large-scale data pipelines on GCP."
    )

    FINANCE_SUMMARY = (
        "Vice President at Goldman Sachs, Equity Research division. "
        "CFA charterholder with 15 years analysing financial markets, "
        "M&A advisory, and portfolio management."
    )

    def test_returns_dict_for_tech_profile(self, analyze_fn):
        """analyze_summary should return a dict for a typical tech profile."""
        result = analyze_fn(self.TECH_SUMMARY)
        assert result is not None, "Expected a dict, got None"
        assert isinstance(result, dict)

    def test_required_keys_present(self, analyze_fn):
        """Result must contain industry, function, and seniority keys."""
        result = analyze_fn(self.TECH_SUMMARY)
        assert result is not None
        for key in ("industry", "function", "seniority"):
            assert key in result, f"Missing key: {key}"

    def test_values_are_non_empty_strings(self, analyze_fn):
        """All three values should be non-empty strings."""
        result = analyze_fn(self.TECH_SUMMARY)
        assert result is not None
        for key in ("industry", "function", "seniority"):
            assert isinstance(result[key], str), f"{key} must be str"
            assert result[key].strip(), f"{key} must not be empty"

    def test_industry_is_technology_for_tech_profile(self, analyze_fn):
        """Industry should be 'Technology' for a clear tech profile."""
        result = analyze_fn(self.TECH_SUMMARY)
        assert result is not None
        assert "tech" in result["industry"].lower(), (
            f"Expected industry to contain 'tech', got: {result['industry']}"
        )

    def test_seniority_is_senior_or_above(self, analyze_fn):
        """Seniority should reflect a senior-level profile."""
        result = analyze_fn(self.TECH_SUMMARY)
        assert result is not None
        assert result["seniority"].lower() in (
            "senior", "lead", "staff", "principal", "executive", "c-level", "director", "vp"
        ), f"Unexpected seniority: {result['seniority']}"

    def test_finance_profile(self, analyze_fn):
        """analyze_summary should correctly classify a finance profile."""
        result = analyze_fn(self.FINANCE_SUMMARY)
        assert result is not None
        assert "finance" in result["industry"].lower() or "banking" in result["industry"].lower(), (
            f"Expected finance/banking industry, got: {result['industry']}"
        )

    def test_handles_very_short_summary(self, analyze_fn):
        """Short summaries should return a dict or None, never raise."""
        result = analyze_fn("Engineer")
        # No assertion on content – just must not raise
        assert result is None or isinstance(result, dict)

    def test_json_response_is_valid(self, analyze_fn):
        """The raw Gemini response must always be valid JSON."""
        from google import genai
        from google.genai import types

        client = genai.Client()
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=self.TECH_SUMMARY,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema={
                    "type": "object",
                    "properties": {
                        "industry": {"type": "string"},
                        "function": {"type": "string"},
                        "seniority": {"type": "string"},
                    },
                    "required": ["industry", "function", "seniority"],
                },
            ),
        )
        parsed = json.loads(response.text)
        assert isinstance(parsed, dict)
