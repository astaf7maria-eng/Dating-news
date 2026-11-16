"""Conservative, local triage; a title can never confirm a fresh news event."""

from __future__ import annotations

import re


def screen_candidate(title: str, policy: dict, *, material_type: str | None = None) -> dict:
    """Return an exclusion reason or require text/date verification.

    ``material_type`` is an evidence-based classification supplied by the
    session, not inferred from the presence of a keyword. The title rules
    only skip obvious lists/guides/comparisons before a full-text download.
    """
    if material_type in policy["excluded_types"]:
        return {"status": "excluded", "reason": material_type}
    title = " ".join(title.split())
    guard = policy["campaign_title_guard"]
    folded = title.casefold()
    campaign = (any(term in folded for term in guard["actions"])
                and any(term in folded for term in guard["objects"]))
    if not campaign:
        for rule in policy["title_rules"]:
            if re.search(rule["pattern"], title, flags=re.IGNORECASE):
                return {"status": "excluded", "reason": rule["reason"]}
    return {"status": "needs_review", "reason": "verify_material_type_new_event_and_publication_date"}
