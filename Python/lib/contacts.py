"""Contact helpers shared by `linkedinmcp`, `webapp` and `lib.get_activity`."""

#: Industry labels the outreach targets (`OUTREACH_TARGET_INDUSTRIES` overrides it in the service).
TARGET_INDUSTRIES = ["RCM", "Pathology", "Medical Lab", "Physician Practice"]


def headline(extracted: dict) -> str | None:
    """`occupation` (Unipile-fetched profiles), else `miniProfile.headline` (LinkedIn Helper)."""
    mini = extracted.get("miniProfile")
    return extracted.get("occupation") or (mini.get("headline") if isinstance(mini, dict) else None) or None


def activity_key(analysis: dict):
    """Sort key, newest first with `reverse=True`: the later of last reply and last sent; neither sorts last."""
    candidates = [d for d in (analysis.get("last_reply_date"), analysis.get("last_sent_date")) if d is not None]
    latest = max(candidates) if candidates else None
    return (latest is not None, latest)
