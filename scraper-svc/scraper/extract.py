"""Post-extraction quality gates for boilerplate detection, completeness, and block page detection.

Runs after readability/markdownify conversion. Returns a quality score (0.0–1.0)
and structured breakdown. Non-blocking — consumers set their own tolerance.
"""

import logging
import re
from dataclasses import dataclass

from .settings import load_settings

logger = logging.getLogger(__name__)

# Default thresholds — overridable via env vars
_ext_settings = load_settings()
MIN_CONTENT_CHARS = _ext_settings.qa_min_content_chars
MIN_TITLE_CHARS = _ext_settings.qa_min_title_chars
MAX_BOILERPLATE_RATIO = _ext_settings.qa_max_boilerplate_ratio

# Block page signatures — patterns that indicate a page rendered but isn't useful content.
# These catch barriers that made it past pre-extraction pattern matching (ADR-0015).
BLOCK_PAGE_PATTERNS: list[re.Pattern] = [
    # JavaScript requirements
    re.compile(r"please enable javascript"),
    re.compile(r"enable javascript to continue"),
    re.compile(r"javascript is required"),
    re.compile(r"please turn javascript on"),
    # Bot challenges
    re.compile(r"please verify you are (?:a )?human"),
    re.compile(r"we need to make sure you(?:'re| are) not a robot"),
    re.compile(r"checking your browser"),
    re.compile(r"we are checking your browser"),
    # Access control
    re.compile(r"access denied"),
    re.compile(r"you have been blocked"),
    re.compile(r"your (?:IP|access) has been blocked"),
    # Rate limiting
    re.compile(r"too many requests"),
    re.compile(r"rate limit"),
    # Session
    re.compile(r"your session has expired"),
    re.compile(r"session timed? ?out"),
    # Generic block page
    re.compile(r"cloudflare"),
    re.compile(r"attention required"),
    re.compile(r"just a moment"),
    # Cookies
    re.compile(r"cookies are required"),
    re.compile(r"please accept cookies"),
    re.compile(r"cookie consent"),
    re.compile(r"this site uses cookies"),
    # Geo-restriction
    re.compile(r"not available in your country"),
    re.compile(r"not available in your region"),
    re.compile(r"geo.?restriction"),
    re.compile(r"content (?:is )?not available"),
    # Paywalls
    re.compile(r"subscribe to continue"),
    re.compile(r"sign up to read more"),
    re.compile(r"members.?only"),
    re.compile(r"this content is for (?:members|subscribers)"),
    # Error pages
    re.compile(r"404 not found"),
    re.compile(r"403 forbidden"),
    re.compile(r"500 error"),
    re.compile(r"internal server error"),
    re.compile(r"page not found"),
    re.compile(r"something went wrong"),
    # CAPTCHA
    re.compile(r"hcaptcha"),
    re.compile(r"recaptcha"),
    re.compile(r"captcha"),
    # Maintenance
    re.compile(r"under maintenance"),
    re.compile(r"temporarily unavailable"),
]


@dataclass
class QualityGateResult:
    """Result of running all quality gates on extracted content.

    Attributes:
        score: Composite quality score (0.0–1.0), higher is better.
        checks: Per-gate status: "pass", "warn", or "fail".
        detail: Human-readable explanation.
    """

    score: float
    checks: dict[str, str]
    detail: str = ""


def _check_boilerplate(markdown: str) -> tuple[float, str]:
    """Detect boilerplate-heavy content.

    Analyzes link density and paragraph quality to determine whether
    the content is mostly navigation/templates rather than article text.

    Returns:
        Tuple of (score 0.0–1.0, status "pass"|"warn"|"fail").
    """
    if not markdown or not markdown.strip():
        return 0.0, "fail"

    lines = markdown.strip().split("\n")
    non_empty = [line.strip() for line in lines if line.strip()]

    if not non_empty:
        return 0.0, "fail"

    # Count link-heavy lines
    link_lines = sum(1 for line in non_empty if re.search(r"\[.*?\]\(.*?\)", line))
    link_ratio = link_lines / len(non_empty)

    # Count substantive paragraphs (multi-sentence, non-link lines)
    substantive = sum(
        1
        for line in non_empty
        if len(line) > 60 and not re.search(r"\[.*?\]\(.*?\)", line)
    )

    # Score
    if link_ratio > MAX_BOILERPLATE_RATIO and substantive < 2:
        return 0.2, "fail"
    elif link_ratio > 0.5 and substantive < 2:
        return 0.4, "warn"
    elif substantive >= 5:
        return 1.0, "pass"
    elif substantive >= 3:
        return 0.85, "pass"
    elif substantive >= 1:
        return 0.6, "warn"
    else:
        return 0.3, "fail"


def _check_completeness(markdown: str, title: str = "") -> tuple[float, str]:
    """Check whether extracted content meets minimum completeness thresholds.

    Args:
        markdown: Extracted markdown content.
        title: Page title if available (from frontmatter or metadata).

    Returns:
        Tuple of (score 0.0–1.0, status "pass"|"warn"|"fail").
    """
    if not markdown or not markdown.strip():
        return 0.0, "fail"

    content = markdown.strip()
    content_len = len(content)

    # Check title quality
    title_ok = (
        len(title.strip()) >= MIN_TITLE_CHARS
        if title
        else content_len >= MIN_CONTENT_CHARS
    )

    # Check paragraph structure
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    has_paragraphs = len(paragraphs) >= 2

    if content_len < MIN_CONTENT_CHARS and not has_paragraphs:
        return 0.1, "fail"
    elif content_len < MIN_CONTENT_CHARS:
        return 0.3, "warn"
    elif not has_paragraphs:
        return 0.4, "warn"
    elif content_len >= 1000 and title_ok and has_paragraphs:
        return 1.0, "pass"
    elif content_len >= 500 and has_paragraphs:
        return 0.85, "pass"
    else:
        return 0.6, "warn"


def _check_block_page(markdown: str, url: str = "") -> tuple[float, str]:
    """Detect block pages, error pages, and other non-content responses.

    Uses pattern matching against known block page signatures. This complements
    the pre-extraction barrier detection in fetch.py by catching barriers that
    rendered as text and made it through to the content pipeline.

    Args:
        markdown: Extracted markdown content.
        url: Source URL (for context, not currently used in pattern matching).

    Returns:
        Tuple of (score 0.0–1.0, status "pass"|"warn"|"fail").
        Higher score = less likely to be a block page.
    """
    if not markdown or not markdown.strip():
        return 0.0, "fail"

    content_lower = markdown.lower()
    matched_patterns = []

    for pattern in BLOCK_PAGE_PATTERNS:
        if pattern.search(content_lower):
            matched_patterns.append(pattern.pattern)

    if matched_patterns:
        count = len(matched_patterns)
        if count >= 3:
            return 0.05, "fail"
        elif count >= 2:
            return 0.15, "fail"
        else:
            return 0.3, "warn"

    # Short content with error keywords
    if len(markdown.strip()) < MIN_CONTENT_CHARS:
        error_words = [
            "error",
            "not found",
            "forbidden",
            "unauthorized",
            "timeout",
            "failed",
            "unavailable",
            "exception",
        ]
        error_count = sum(1 for w in error_words if w in content_lower)
        if error_count >= 2:
            return 0.2, "fail"

    return 1.0, "pass"


def assess_quality(
    markdown: str,
    html: str = "",
    url: str = "",
    title: str = "",
) -> dict:
    """Run all quality gates on extracted content.

    Lightweight heuristic assessment — no external APIs or LLM calls.

    Args:
        markdown: Extracted markdown content to assess.
        html: Raw HTML if available (for future heuristics).
        url: Source URL (for context).
        title: Page title if available.

    Returns:
        Dict with score, per-check breakdown, and detail:
        {
            "score": 0.95,
            "checks": {"boilerplate": "pass", "completeness": "pass", "block_detected": "pass"},
            "detail": "all checks passed"
        }
    """
    boilerplate_score, boilerplate_status = _check_boilerplate(markdown)
    completeness_score, completeness_status = _check_completeness(markdown, title)
    block_score, block_status = _check_block_page(markdown, url)

    # Weighted composite: boilerplate 30%, completeness 30%, block detection 40%
    overall = boilerplate_score * 0.3 + completeness_score * 0.3 + block_score * 0.4

    # Build detail string
    details = []
    if boilerplate_status != "pass":
        details.append(f"boilerplate:{boilerplate_status}")
    if completeness_status != "pass":
        details.append(f"completeness:{completeness_status}")
    if block_status != "pass":
        details.append(f"block:{block_status}")

    return {
        "score": round(overall, 2),
        "checks": {
            "boilerplate": boilerplate_status,
            "completeness": completeness_status,
            "block_detected": block_status,
        },
        "detail": ", ".join(details) if details else "all checks passed",
    }
