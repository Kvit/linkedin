"""
Live tests for the Gemini classifier used by analysis.ipynb and new-contacts.ipynb.

Verifies that the model honours the response_mime_type="application/json" +
response_schema contract (the Literal-typed ProfileAnalysis) and that clear-cut
profiles land on the labels the taxonomy rules prescribe. The contract is
imported from `profiles` rather than copied, so the prompt under test is the
prompt in production.
"""

import os

import pytest
from dotenv import load_dotenv
from google import genai

from profiles import MODEL, ProfileAnalysis, generation_config

load_dotenv()

# ---------------------------------------------------------------------------
# Shared config -- imported from profiles.py, the module in production
# ---------------------------------------------------------------------------

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
    "practice_lab_manager": (
        "Laboratory Manager at Lakeside Endocrinology Associates, a 12-physician "
        "practice with an in-office CLIA-certified laboratory. Oversees phlebotomy, "
        "testing workflow and laboratory billing."
    ),
    "hospital_lab_director": (
        "Medical Director of Laboratory Medicine at Mercy Regional Hospital, a 400-bed "
        "community hospital. Board-certified clinical pathologist."
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
    return generation_config()


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
    """Spot-check that field *values* follow the taxonomy rules for clear-cut profiles."""

    def test_tech_profile(self, client, structured_config):
        result = call(client, PROFILES["tech_senior"], structured_config)
        assert result["industry"] == "Non-Healthcare", result
        assert result["function"] == "IT", result
        # "Senior Software Engineer" who "led teams": individual contributor or team lead.
        assert result["seniority"] in ("Staff", "Manager"), result

    def test_finance_profile_is_other_not_finance(self, client, structured_config):
        """Investment banking is Other; Finance is reserved for finance roles inside operating organizations."""
        result = call(client, PROFILES["finance_vp"], structured_config)
        assert result["industry"] == "Non-Healthcare", result
        assert result["function"] == "Other", result
        assert result["seniority"] == "VP", result

    def test_healthcare_profile(self, client, structured_config):
        result = call(client, PROFILES["healthcare_entry"], structured_config)
        assert result["industry"] == "Hospital", result
        assert result["function"] == "Clinical", result
        assert result["seniority"] == "Staff", result

    def test_marketing_profile(self, client, structured_config):
        result = call(client, PROFILES["marketing_director"], structured_config)
        assert result["industry"] == "Non-Healthcare", result
        assert result["function"] == "Sales & Marketing", result
        assert result["seniority"] == "Director", result

    def test_practice_owned_lab_is_medical_lab(self, client, structured_config):
        """A laboratory owned by a physician practice is a target: Medical Lab, not Physician Practice."""
        result = call(client, PROFILES["practice_lab_manager"], structured_config)
        assert result["industry"] == "Medical Lab", result
        assert result["function"] == "Operations", result
        assert result["seniority"] == "Manager", result

    def test_hospital_owned_lab_is_hospital(self, client, structured_config):
        """A hospital's laboratory is not a target: it stays under Hospital."""
        result = call(client, PROFILES["hospital_lab_director"], structured_config)
        assert result["industry"] == "Hospital", result
        assert result["function"] == "Clinical", result


# ---------------------------------------------------------------------------
# 3. Consistency -- same input yields identical output
# ---------------------------------------------------------------------------


class TestConsistency:
    """Two calls with the same clear-cut profile must return the same labels."""

    def test_same_keys_across_calls(self, client, structured_config):
        r1 = call(client, PROFILES["tech_senior"], structured_config)
        r2 = call(client, PROFILES["tech_senior"], structured_config)
        assert set(r1.keys()) == set(r2.keys()), (
            f"Key sets differ between calls: {r1.keys()} vs {r2.keys()}"
        )

    def test_same_labels_across_calls(self, client, structured_config):
        r1 = call(client, PROFILES["finance_vp"], structured_config)
        r2 = call(client, PROFILES["finance_vp"], structured_config)
        assert r1 == r2, f"Labels differ between calls: {r1} vs {r2}"
