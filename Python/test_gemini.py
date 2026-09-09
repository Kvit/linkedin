"""
Live tests for the Gemini classifier used by analysis.ipynb and new-contacts.ipynb.

Verifies that the model honours the response_mime_type="application/json" +
response_schema contract (the Literal-typed ProfileAnalysis) and that clear-cut
profiles land on the labels the taxonomy rules prescribe.
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

MODEL = "gemini-3.8-flash"

ANALYSIS_INSTRUCTIONS = """You classify LinkedIn profiles for a healthcare RCM technology company. The goal is to find potential buyers of Recovr by Pinnacle Services, AI-powered claim denial recovery software sold to pathology practices, independent and physician-practice-owned medical laboratories, and revenue cycle management (RCM) and medical billing companies. Hospital-owned laboratories are not a target, so the industry labels below separate laboratories by who owns them.

# Input
You receive one flattened LinkedIn profile as plain text. It is a mechanical dump, not prose: it may contain field names, quotation marks, timestamps, numeric ids, image URLs and repeated values. Ignore all of that and read only the professional content: headline, roles and employers, role descriptions, About text, skills, certifications and education.

The profile may list many roles, past and present.

# Rule 1: classify the CURRENT role
industry, function and seniority all describe the person's current position.
- Current means the role with no end date, or the most recently started role if none is open-ended. If several roles have no end date, use the one the headline describes; otherwise the most recently started.
- If the person is retired, between roles, or lists only past roles, use the most recent role.
- Use earlier roles, the About text, skills and certifications only to resolve ambiguity in the current role, never to override it. A former nurse who now leads a coding team is Operations, not Clinical.

# Rule 2: industry is the EMPLOYER'S business, not the person's job
A billing manager employed by a hospital is "Hospital". A sales representative employed by a laboratory is "Medical Lab".

industry, exactly one of:
- "RCM": revenue cycle management, medical billing, coding, CDI, HIM, denial management or collections companies, including RCM consultancies and outsourcing arms
- "Pathology": pathology practices and groups (anatomic, clinical, dermatopathology, histology), including the laboratories they operate. When both Pathology and Medical Lab fit, use Pathology.
- "Medical Lab": laboratories that test human specimens for clinical purposes and are NOT owned by a hospital: independent labs (Quest, Labcorp, regional reference, molecular, genetic and toxicology labs) and laboratories owned by a physician practice. A practice owns a lab when the profile shows it: a laboratory title (laboratory director, administrator, manager, technologist), CLIA, in-office or in-house testing, or a described laboratory. Cannabis, food, environmental, forensic and industrial testing labs are "Non-Healthcare".
- "Physician Practice": physician practices, medical groups, clinics, urgent care, ambulatory surgery centers and therapy practices with no laboratory in evidence
- "Hospital": hospitals, health systems and academic medical centers, including their laboratories, pathology departments and billing departments
- "Health IT": healthcare software, health technology and data or analytics vendors serving healthcare
- "Health Insurance Payer": insurers, health plans, managed care and other payer-side organizations
- "Pharma": pharmaceutical, biotech, medical device and laboratory instrument or reagent companies
- "Other Healthcare": healthcare-adjacent organizations not listed above, such as staffing, healthcare consulting firms, associations, research institutes, non-profits, home health, behavioral health and senior care
- "Non-Healthcare": not in healthcare

# Rule 3: function is the domain of the work
function, exactly one of:
- "Operations": revenue cycle, billing, coding, CDI, HIM, claims, denials, collections, patient access, practice or laboratory operations, quality and compliance
- "Finance": finance, accounting, controller, FP&A, CFO and other financial leadership inside an operating organization. Investment, private equity, banking, M&A and corporate development roles are "Other", not Finance.
- "IT": software, IT, data, engineering, informatics, CIO and CTO
- "Clinical": practising clinicians in a care-delivery or diagnostic role: physicians, pathologists, nurses, laboratory technologists, therapists
- "Executive": general management only: CEO, President, Managing Director, General Manager, Executive Director, board member. Never use it for an executive who leads a specific function.
- "Consulting": delivers consulting or advisory services as their role
- "Owner": use only when the person owns or founded the business and nothing indicates what they do day to day
- "Sales & Marketing": sales, business development, partnerships, marketing, account management and customer success
- "Other": none of the above

Functional executives take their domain, not "Executive": CFO is Finance; COO is Operations; CIO or CTO is IT; Chief Revenue Officer or Chief Marketing Officer is Sales & Marketing; Chief Medical Officer or Chief Nursing Officer is Clinical; Chief Revenue Cycle Officer or VP of Revenue Cycle is Operations. A General Manager of a region, segment or business unit takes the domain the headline or description emphasises, and is "Executive" only when nothing narrower applies. A founder takes the domain the headline or description shows: one who builds the product is IT, one who sells is Sales & Marketing, one who runs the company as CEO or President is Executive. A consultant at a consulting firm is Consulting, but a VP of Sales at a consulting firm is Sales & Marketing.

# Rule 4: seniority from the title
seniority, exactly one of:
- "Owner": owner, founder, co-founder, partner or principal of their own firm. This takes precedence over every other level: a founder and CEO is "Owner".
- "Executive": C-level (Chief X Officer), President, EVP, SVP, Managing Director, Executive Director, General Manager of a whole company
- "VP": Vice President, AVP, head of a function at a large organization, department Chair or Medical Director at a hospital or academic medical center, General Manager of a region or business unit
- "Director": Director, Senior Director, Associate Director, head of a team
- "Manager": Manager, Supervisor, Team Lead, Practice Administrator, Laboratory Manager
- "Staff": individual contributors: analyst, specialist, coordinator, representative, technologist, nurse, physician or consultant without a leadership title
- "Unknown": no title information at all

Board members, advisors and retired people take their most recent operating role; if none is given, use "Executive".

# Examples
- Chief Financial Officer, Sunrise Pathology Associates -> Pathology / Finance / Executive
- Founder and CEO, ClaimPath Billing Services -> RCM / Executive / Owner
- VP Coding Quality at a medical coding services company, formerly a hospital CDI nurse -> RCM / Operations / VP
- Regional Sales Director, Quest Diagnostics -> Medical Lab / Sales & Marketing / Director
- Histotechnologist, University Hospital -> Hospital / Clinical / Staff
- Practice Administrator, Coastal Dermatology -> Physician Practice / Operations / Manager
- Laboratory Administrator at a multi-physician endocrinology practice -> Medical Lab / Operations / Manager
- Chair of Pathology and Laboratory Medicine, academic medical center -> Hospital / Clinical / VP
- Controller, regional reference laboratory -> Medical Lab / Finance / Manager
- Vice President, healthcare private equity fund -> Non-Healthcare / Other / VP
- Revenue Cycle Manager, Memorial Health System -> Hospital / Operations / Manager

# Output
Return JSON with exactly the keys industry, function and seniority, using only the values listed above. Use "Other" or "Unknown" only when the profile gives no basis for a decision. Never return an empty string."""


from typing import Literal

# The controlled vocabulary lives in the schema Gemini must satisfy, so an
# out-of-vocabulary label fails validation instead of landing in Firestore.
Industry = Literal[
    "RCM", "Pathology", "Medical Lab", "Physician Practice", "Hospital", "Health IT",
    "Health Insurance Payer", "Pharma", "Other Healthcare", "Non-Healthcare",
]
Function = Literal[
    "Operations", "Finance", "IT", "Clinical", "Executive", "Consulting", "Owner",
    "Sales & Marketing", "Other",
]
Seniority = Literal["Executive", "VP", "Director", "Manager", "Staff", "Owner", "Unknown"]


class ProfileAnalysis(BaseModel):
    industry: Industry
    function: Function
    seniority: Seniority


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
    return types.GenerateContentConfig(
        system_instruction=ANALYSIS_INSTRUCTIONS,
        response_mime_type="application/json",
        response_schema=ProfileAnalysis,
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
