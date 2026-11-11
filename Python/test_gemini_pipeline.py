"""
Live tests for the pipeline classifier behind pipeline-classify.py.

Verifies that the model honours the response_schema contract (the Literal-typed
PipelineAnalysis) and that clear-cut conversations land on the stage the rules
prescribe. The contract is imported from `pipeline` rather than copied, so the
prompt under test is the prompt in production.

Every call runs inside one session fixture, for two reasons: the client's async
HTTP pool binds to the first event loop it runs on, so one `asyncio.run` per
client is the rule; and the twenty-two assertions then cost eleven calls, not
twenty-two.

Not collected by a bare `uv run pytest` (testpaths is `tests/`). Run by name:

    uv run pytest test_gemini_pipeline.py -v

Skips without GOOGLE_API_KEY. PIPELINE_THINKING_LEVEL=medium (or high) runs the
same probes at another level, to check before changing the default.
"""

import asyncio
import os

import pytest
from dotenv import load_dotenv

from pipeline import PipelineAnalysis, classify_conversation, gemini_client

load_dotenv()

INTRO = (
    "Thanks for connecting. If you ever have any questions about using AI for "
    "automatic recovery of medical claims denials, I'd be happy to help, even if "
    "it's just asking advice. Let's stay in touch!"
)

# `analysis` documents as `load_contacts` returns them: the summary plus the
# classification already on file, which the prompt shows as given.
PATHOLOGY = {
    "summary": (
        "Director of Revenue Cycle at Summit Pathology Partners, a 14-pathologist "
        "anatomic pathology group with its own histology laboratory."
    ),
    "industry": "Pathology",
    "function": "Operations",
    "seniority": "Director",
}
HOSPITAL = {
    "summary": (
        "Vice President of Revenue Cycle at Mercy Regional Hospital, a 400-bed "
        "community hospital with an outreach laboratory program."
    ),
    "industry": "Hospital",
    "function": "Operations",
    "seniority": "VP",
}
RECRUITER = {
    "summary": (
        "Senior Technical Recruiter at TalentBridge Partners, placing engineering "
        "leaders in venture-backed health-tech companies."
    ),
    "industry": "Non-Healthcare",
    "function": "Other",
    "seniority": "Staff",
}
BETWEEN_ROLES = {
    "summary": (
        "Revenue Cycle Manager. Fifteen years in physician-practice billing, "
        "denials management and payer follow-up."
    ),
    "industry": "Physician Practice",
    "function": "Operations",
    "seniority": "Manager",
}
CONTRACTOR = {
    "summary": (
        "Independent consultant and speaker on medical coding and compliance. "
        "Leads webinars and presentations for RCM industry audiences."
    ),
    "industry": "RCM",
    "function": "Consulting",
    "seniority": "Staff",
}


def _chat(*lines):
    return "--- conversation 1 ---\n" + "\n".join(lines)


# name -> (contact, transcript). One per stage, then the rule probes.
CONVERSATIONS = {
    "lead_asks_how_it_works": (PATHOLOGY, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Interesting. We write off a lot of Medicare denials every "
        "month. How does your tool decide which ones to appeal, and what does it cost?",
    )),
    # A courtesy reply moves nothing: the contact keeps the default stage the
    # industry / seniority targeting gave them.
    "prospect_courtesy_reply": (PATHOLOGY, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Thanks Vitali, nice to connect. I appreciate the offer "
        "and will keep it in mind. Let's keep in touch.",
    )),
    "reject_has_vendor": (PATHOLOGY, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Appreciate the note, but we're all set -- our billing "
        "company handles denials and we're not looking to change anything.",
    )),
    "not_relevant_recruiter": (RECRUITER, _chat(
        "2026-03-01 Them: Hi Vitali, I'm hiring a VP of Engineering for a Series B "
        "health-tech startup and your background looks like a great fit. Open to a chat?",
        "2026-03-02 Me: Thanks, not looking at the moment.",
    )),
    # A refusal whose reason can expire keeps the relationship: these contacts
    # still get product updates, so they must not land in `reject` or
    # `not_relevant`. All three are drawn from real rulings that came back
    # wrong under the five-stage vocabulary.
    "soft_no_between_roles": (BETWEEN_ROLES, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Thanks for reaching out. Unfortunately my position was "
        "eliminated last month, so I'm not with the practice any more and am "
        "exploring what's next.",
    )),
    "soft_no_not_the_decision_maker": (CONTRACTOR, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Happy to connect! I should say I'm project-based here, "
        "just running the webinars and presentations, so I'm not in a position to "
        "suggest tools like yours.",
    )),
    "soft_no_bad_timing": (PATHOLOGY, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Appreciate it, but my availability right now doesn't "
        "allow me to take on anything else at the moment.",
    )),
    # The other side of that boundary: "at this time" hedging a statement of
    # disinterest is politeness, not a circumstance. Contrast with
    # soft_no_bad_timing, where availability -- not interest -- is the obstacle.
    "reject_politely_hedged": (PATHOLOGY, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Thank you for reaching out, but we are not interested "
        "at this time.",
    )),
    # Rule probes.
    "reject_after_interest": (PATHOLOGY, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Sure, send me a one-pager, we do have a denials backlog.",
        "2026-03-03 Me: Here is the overview and pricing.",
        "2026-03-10 Them: Thanks for sending. We reviewed it and decided to stay "
        "with our current process. No need to follow up.",
    )),
    "prospect_sounds_good": (PATHOLOGY, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: Sounds good!",
    )),
    # The ICP boundary: "Hospital" is on file and visible, and must not decide.
    "lead_at_a_hospital": (HOSPITAL, _chat(
        f"2026-03-01 Me: {INTRO}",
        "2026-03-02 Them: We have a real denials problem in our lab outreach program. "
        "Can you send details on how it works and whether it runs against Epic?",
    )),
}

EXPECTED = {
    "lead_asks_how_it_works": "lead",
    "prospect_courtesy_reply": "prospect",
    "reject_has_vendor": "reject",
    "not_relevant_recruiter": "not_relevant",
    "soft_no_between_roles": "soft_no",
    "soft_no_not_the_decision_maker": "soft_no",
    "soft_no_bad_timing": "soft_no",
    "reject_politely_hedged": "reject",
    "reject_after_interest": "reject",
    "prospect_sounds_good": "prospect",
    "lead_at_a_hospital": "lead",
}

THINKING_LEVEL = os.environ.get("PIPELINE_THINKING_LEVEL", "low")


@pytest.fixture(scope="session")
def results() -> dict[str, PipelineAnalysis]:
    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY not set")
    client = gemini_client()

    async def run_all():
        names = list(CONVERSATIONS)
        outcomes = await asyncio.gather(
            *(
                classify_conversation(
                    client, *CONVERSATIONS[name], thinking_level=THINKING_LEVEL
                )
                for name in names
            )
        )
        return dict(zip(names, outcomes))

    return asyncio.run(run_all())


@pytest.mark.parametrize("name", list(CONVERSATIONS))
def test_response_satisfies_the_schema(results, name):
    result = results[name]

    assert isinstance(result, PipelineAnalysis), name
    assert result.reason.strip(), f"[{name}] reason is empty"


@pytest.mark.parametrize("name,expected", EXPECTED.items())
def test_clear_cut_conversations_land_on_the_prescribed_stage(results, name, expected):
    assert results[name].stage == expected, f"[{name}] {results[name]}"
