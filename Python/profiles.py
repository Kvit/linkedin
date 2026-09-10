"""The profile classifier `analysis.ipynb` and `new-contacts.ipynb` run inline,
lifted out here so the scheduled outreach service can import it instead of
holding a third copy.

Both notebooks keep their own copies of the prompt, the vocabulary and the
schema below, because the user runs them by hand. This module exists only for
the service, which classifies the day's new connections on the exact contract
that already labelled 14,158 contacts, not a reimplementation that could
quietly drift from it.

`pipeline.py` is the analogue for conversations: same project, same Gemini
SDK, same structured-output contract -- a Literal-typed Pydantic schema
enforced through `response_schema`. Read it first for the house style.
"""

import logging
from typing import Literal

from google.cloud import firestore
from google.genai import types
from pydantic import BaseModel

from functions import get_member_distance

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


#: A shorter summary is a stub LinkedIn withheld, not a profile, and
#: classifying it would waste a call. Both notebooks use this same threshold.
SUMMARY_MIN_LEN = 50


def generation_config() -> types.GenerateContentConfig:
    """The call configuration the notebooks build, holding no connection of its own."""
    # No trailing comma after the closing paren: that would make `config` a tuple
    # and the SDK would fail with "'tuple' object has no attribute 'tools'".
    # AFC is disabled because this call passes no tools; it also silences the
    # SDK's once-per-process automatic-function-calling advisory.
    return types.GenerateContentConfig(
        system_instruction=ANALYSIS_INSTRUCTIONS,
        response_mime_type="application/json",
        response_schema=ProfileAnalysis,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


logger = logging.getLogger("profiles")


def classify_profile(client, summary: str) -> ProfileAnalysis | None:
    """One Gemini call, classifying a single flattened LinkedIn profile.

    `client` should be built with `pipeline.gemini_client()`, which turns on
    retries and a 60-second timeout; this function does not build one itself,
    so a batch job and a one-profile caller can share whichever client they
    already hold.

    Returns None on any failure -- an API error, a response the SDK could not
    parse, or a label outside the controlled vocabulary -- so a caller can
    count failures without wrapping every call in its own try block.
    `response.parsed` is read first, since that is the SDK's own validated
    instance of `ProfileAnalysis`; `response.text` is read only when the SDK
    left `.parsed` unset.

    A failure logs the error alongside the first 100 characters of the summary,
    as the notebook this replaced printed them. Unattended, a warning that says
    only that something failed is not worth having. The caller knows the
    document id and should log that too; this function is only handed the text.
    """
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=summary,
            config=generation_config(),
        )
        if isinstance(response.parsed, ProfileAnalysis):
            return response.parsed
        return ProfileAnalysis.model_validate_json(response.text)
    except Exception as error:
        preview = summary[:100] + "..." if len(summary) > 100 else summary
        logger.warning(
            "classify_profile failed: %s: %s | summary: %s",
            type(error).__name__,
            error,
            preview,
        )
        return None


def analysis_body(extracted_doc: dict, result: ProfileAnalysis) -> dict:
    """The merge document `new-contacts.ipynb` Phase E writes to `analysis`.

    Always written with `merge=True`, never a plain `set()`: `analysis`
    documents also carry contact names, emails and message tallies that exist
    nowhere else, and a `set()` without `merge=True` would replace the whole
    document with just these eight fields and destroy them.
    """
    return {
        "profileUrl": extracted_doc.get("profileUrl", "") or "",
        "lh_id": str(extracted_doc.get("lhId", "") or ""),
        "industry": result.industry,
        "function": result.function,
        "seniority": result.seniority,
        "summary": (extracted_doc.get("summary") or "").replace("\n", " "),
        "memberDistance": get_member_distance(extracted_doc),
        "created_at": firestore.SERVER_TIMESTAMP,
    }
