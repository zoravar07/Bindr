"""whyStopped classifier for ClinicalTrials.gov termination rationales."""

from __future__ import annotations
import re
from typing import Literal
from pydantic import BaseModel

FailureCategory = Literal[
    "ACCRUAL_RECRUITMENT",
    "EFFICACY_FUTILITY",
    "SAFETY_TOXICITY",
    "COMMERCIAL_STRATEGIC",
    "OPERATIONAL_REGULATORY",
    "OTHER_UNKNOWN",
]


class ClassificationResult(BaseModel):
    category: FailureCategory
    confidence: float
    matched_keywords: list[str]
    raw_text: str


PATTERNS: dict[FailureCategory, list[str]] = {
    "ACCRUAL_RECRUITMENT": [
        r"\b(accru\w*|recruit\w*|enroll\w*)\b",
        r"\bscreen(ing)?\s+failure\b",
        r"\b(participant|subject|patient)(s)?\b",
        r"\bfeasibility\b",
        r"\b(retain|retention)\b",
        r"\blagged\b",
        r"\bclosed\s+to\s+enrollment\b",
        r"\bdrop(-)?out(s)?\b",
    ],
    "EFFICACY_FUTILITY": [
        r"\befficac\w*\b",
        r"\bfutil\w*\b",
        r"\bendpoint(s)?\b",
        r"\binterim\b",
        r"\bconditional\s+power\b",
        r"\b(control|placebo)\b",
        r"\bprogression-free\b",
        r"\boverall\s+survival\b",
        r"\bresponse\s+rate\b",
        r"\bfailed\s+to\s+meet\b",
        r"\boutcome\s+measure\b",
        r"\btumor\s+response\b",
    ],
    "SAFETY_TOXICITY": [
        r"\bsafet\w*\b",
        r"\btoxici\w*\b",
        r"\badverse\b",
        r"\btolerab\w*\b",
        r"\bmortalit\w*\b",
        r"\bdeath(s)?\b",
        r"\btransaminase(s)?\b",
        r"\bcytokine\b",
        r"\bhyperkalemia\b",
        r"\bthrombocytopenia\b",
        r"\bqt\b",
        r"\binfection(s)?\b",
        r"\bdose-limiting\b",
        r"\bgrade\s+[345]\b",
    ],
    "COMMERCIAL_STRATEGIC": [
        r"\bbusiness\b",
        r"\bsponsor\b",
        r"\bcommercial\b",
        r"\breprioriti\w*\b",
        r"\bportfolio\b",
        r"\bpipeline\b",
        r"\bbankrupt\w*\b",
        r"\bmerger\b",
        r"\bfund\w*\b",
        r"\bfinancial\b",
        r"\bstrategic\b",
        r"\bcompany\b",
        r"\bbudget\b",
        r"\bcompetitor\b",
        r"\bdiscontinued\b",
    ],
    "OPERATIONAL_REGULATORY": [
        r"\bregulat\w*\b",
        r"\bfda\b",
        r"\bgcp\b",
        r"\baudit\b",
        r"\birb\b",
        r"\bprotocol\b",
        r"\bpi\b",
        r"\binvestigator\b",
        r"\bcovid\w*\b",
        r"\bcoronavirus\b",
        r"\bsupply\b",
        r"\bmanufactur\w*\b",
        r"\breview\b",
        r"\bexpir\w*\b",
        r"\binactiv\w*\b",
        r"\badministrat\w*\b",
        r"\bschedul\w*\b",
        r"\bsample\w*\b",
        r"\bstudy\s+design\b",
    ],
}


def classify_why_stopped(raw_text: str | None) -> ClassificationResult:
    if not raw_text or not raw_text.strip():
        return ClassificationResult(
            category="OTHER_UNKNOWN",
            confidence=0.0,
            matched_keywords=[],
            raw_text="",
        )

    text = raw_text.strip()
    text_lower = text.lower()
    matches_per_category: dict[FailureCategory, list[str]] = {}

    for cat, regex_list in PATTERNS.items():
        matched = []
        for pat in regex_list:
            found = re.findall(pat, text_lower)
            if found:
                for item in found:
                    token = next((x for x in item if x), "") if isinstance(item, tuple) else item
                    token = token.strip()
                    if token and token not in matched:
                        matched.append(token)
        if matched:
            matches_per_category[cat] = matched

    if not matches_per_category:
        return ClassificationResult(
            category="OTHER_UNKNOWN",
            confidence=0.2,
            matched_keywords=[],
            raw_text=text,
        )

    best_cat = max(matches_per_category.keys(), key=lambda c: len(matches_per_category[c]))
    matched_kws = matches_per_category[best_cat]
    confidence = 0.95 if len(matched_kws) >= 2 else 0.80

    return ClassificationResult(
        category=best_cat,
        confidence=confidence,
        matched_keywords=matched_kws,
        raw_text=text,
    )
