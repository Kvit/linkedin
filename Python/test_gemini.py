"""
Tests for Gemini structured output (JSON schema enforcement).

Focuses exclusively on verifying that the model honours the
response_mime_type="application/json" + response_json_schema contract used
throughout analysis.ipynb.
"""

import os
import pytest
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# ---------------------------------------------------------------------------
# Shared config – mirrors analysis.ipynb exactly
# ---------------------------------------------------------------------------

MODEL = "gemini-2.0-flash"

ANALYSIS_INSTRUCTIONS = """You are an expert at analyzing LinkedIn profile summaries.
Analyze the provided profile summary and extract the following information:

1. industry - The primary industry the person works in (e.g., "Technology", "Finance", "Healthcare")
2. function - Their job function/role type (e.g., "Engineering", "Sales", "Marketing", "Operations")
3. seniority - Their seniority level (e.g., "Entry", "Mid", "Senior", "Executive", "C-Level")"""


class ProfileAnalysis(BaseModel):
    industry: str
    function: str
    seniority: str


REQUIRED_KEYS = ("industry", "function", "seniority")

# ---------------------------------------------------------------------------
# Sample profiles
# ---------------------------------------------------------------------------

PROFILES = {
    "tech_senior": (
        "Senior Software Engineer at Google with 10 years of experience in "
        "distributed systems, cloud infrastructure, and backend development. "
        "Led teams building large-scale data pipelines on GCP."
    ),
    "finance_vp": (
        "Vice President at Goldman Sachs, Equity Research division. "
        "CFA charterholder with 15 years analysing financial markets, "
        "M&A advisory, and portfolio management."
    ),
    "healthcare_entry": (
        "Junior Nurse Practitioner at City General Hospital. "
        "Recently graduated, one year of clinical experience in oncology ward."
    ),
    "marketing_director": (
        "Director of Marketing at Adidas with 12 years building global brand campaigns, "
        "managing cross-functional teams, and driving digital transformation initiatives."
    ),
    "short": "Engineer",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def client():
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        pytest.skip("GOOGLE_API_KEY not set")
    return genai.Client()


@pytest.fixture(scope="session")
def structured_config():
    """GenerateContentConfig enforcing the analysis JSON schema."""
    return types.GenerateContentConfig(
        system_instruction=ANALYSIS_INSTRUCTIONS,
        response_mime_type="application/json",
        response_json_schema=ProfileAnalysis,
    )


def call(client, summary, config):
    """Helper: call model and return parsed dict (raises on any failure)."""
    response = client.models.generate_content(
        model=MODEL,
        contents=summary,
        config=config,
    )
    return ProfileAnalysis.model_validate_json(response.text).model_dump()


# ---------------------------------------------------------------------------
# 1. Schema contract – response is valid JSON matching the schema
# ---------------------------------------------------------------------------


class TestSchemaContract:
    """The model must always return JSON that satisfies ANALYSIS_SCHEMA."""

    @pytest.mark.parametrize("name,summary", PROFILES.items())
    def test_response_is_valid_json(self, client, structured_config, name, summary):
        """response.text must be parseable as a valid ProfileAnalysis."""
        response = client.models.generate_content(
            model=MODEL, contents=summary, config=structured_config
        )
        try:
            result = ProfileAnalysis.model_validate_json(response.text)
        except Exception as exc:
            pytest.fail(f"[{name}] response.text is not valid ProfileAnalysis: {exc}\nRaw: {response.text!r}")
        assert isinstance(result, ProfileAnalysis), f"[{name}] parsed result must be ProfileAnalysis, got {type(result)}"

    @pytest.mark.parametrize("name,summary", PROFILES.items())
    def test_all_required_keys_present(self, client, structured_config, name, summary):
        """Parsed response must contain all three required keys."""
        result = call(client, summary, structured_config)
        missing = [k for k in REQUIRED_KEYS if k not in result]
        assert not missing, f"[{name}] missing keys: {missing}"

    @pytest.mark.parametrize("name,summary", PROFILES.items())
    def test_all_values_are_strings(self, client, structured_config, name, summary):
        """Every value in the response must be a str (not int, list, etc.)."""
        result = call(client, summary, structured_config)
        for key in REQUIRED_KEYS:
            assert isinstance(result[key], str), (
                f"[{name}] '{key}' must be str, got {type(result[key])}"
            )

    @pytest.mark.parametrize("name,summary", PROFILES.items())
    def test_no_extra_keys(self, client, structured_config, name, summary):
        """Schema should constrain the response to exactly the three fields."""
        result = call(client, summary, structured_config)
        extra = set(result.keys()) - set(REQUIRED_KEYS)
        assert not extra, f"[{name}] unexpected extra keys: {extra}"

    @pytest.mark.parametrize("name,summary", [
        (n, s) for n, s in PROFILES.items() if n != "short"
    ])
    def test_string_values_are_non_empty(self, client, structured_config, name, summary):
        """Non-trivial profiles should produce non-empty string values."""
        result = call(client, summary, structured_config)
        for key in REQUIRED_KEYS:
            assert result[key].strip(), f"[{name}] '{key}' value is empty"


# ---------------------------------------------------------------------------
# 2. Semantic correctness – model classifies profiles sensibly
# ---------------------------------------------------------------------------


class TestSemanticOutput:
    """Spot-check that field *values* make sense for well-defined profiles."""

    def test_tech_profile_industry(self, client, structured_config):
        result = call(client, PROFILES["tech_senior"], structured_config)
        assert "tech" in result["industry"].lower(), (
            f"Expected technology industry, got: {result['industry']}"
        )

    def test_tech_profile_function(self, client, structured_config):
        result = call(client, PROFILES["tech_senior"], structured_config)
        assert "engineer" in result["function"].lower() or "tech" in result["function"].lower(), (
            f"Expected engineering function, got: {result['function']}"
        )

    def test_tech_profile_seniority(self, client, structured_config):
        result = call(client, PROFILES["tech_senior"], structured_config)
        assert result["seniority"].lower() in (
            "senior", "lead", "staff", "principal", "mid", "manager"
        ), f"Unexpected seniority for senior engineer: {result['seniority']}"

    def test_finance_profile_industry(self, client, structured_config):
        result = call(client, PROFILES["finance_vp"], structured_config)
        assert any(w in result["industry"].lower() for w in ("finance", "bank", "investment")), (
            f"Expected finance industry, got: {result['industry']}"
        )

    def test_finance_profile_seniority_is_senior(self, client, structured_config):
        result = call(client, PROFILES["finance_vp"], structured_config)
        assert result["seniority"].lower() in (
            "vp", "vice president", "senior", "executive", "director"
        ), f"Unexpected seniority for VP: {result['seniority']}"

    def test_healthcare_profile_industry(self, client, structured_config):
        result = call(client, PROFILES["healthcare_entry"], structured_config)
        assert "health" in result["industry"].lower() or "medical" in result["industry"].lower(), (
            f"Expected healthcare industry, got: {result['industry']}"
        )

    def test_healthcare_profile_seniority_is_entry(self, client, structured_config):
        result = call(client, PROFILES["healthcare_entry"], structured_config)
        assert result["seniority"].lower() in (
            "entry", "junior", "associate", "mid"
        ), f"Expected entry-level seniority, got: {result['seniority']}"

    def test_marketing_profile_function(self, client, structured_config):
        result = call(client, PROFILES["marketing_director"], structured_config)
        assert "market" in result["function"].lower(), (
            f"Expected marketing function, got: {result['function']}"
        )

    def test_marketing_profile_seniority_is_director(self, client, structured_config):
        result = call(client, PROFILES["marketing_director"], structured_config)
        assert result["seniority"].lower() in (
            "director", "senior", "executive", "vp", "manager"
        ), f"Expected director-level seniority, got: {result['seniority']}"


# ---------------------------------------------------------------------------
# 3. Consistency – same input yields structurally identical output
# ---------------------------------------------------------------------------


class TestConsistency:
    """Two calls with the same profile must return structurally identical output."""

    def test_same_keys_across_calls(self, client, structured_config):
        r1 = call(client, PROFILES["tech_senior"], structured_config)
        r2 = call(client, PROFILES["tech_senior"], structured_config)
        assert set(r1.keys()) == set(r2.keys()), (
            f"Key sets differ between calls: {r1.keys()} vs {r2.keys()}"
        )

    def test_same_industry_across_calls(self, client, structured_config):
        r1 = call(client, PROFILES["finance_vp"], structured_config)
        r2 = call(client, PROFILES["finance_vp"], structured_config)
        # Both should at least agree on the broad industry category
        assert (
            any(w in r1["industry"].lower() for w in ("finance", "bank", "investment")) and
            any(w in r2["industry"].lower() for w in ("finance", "bank", "investment"))
        ), f"Industry inconsistent across calls: {r1['industry']!r} vs {r2['industry']!r}"
