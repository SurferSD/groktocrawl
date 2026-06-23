"""Natural-language to crawl-parameters translation.

Uses the existing ``LLMClient`` to translate a user's natural-language
description of what they want to crawl into structured crawl parameters
(``includePaths``, ``excludePaths``, ``maxDepth``, etc.).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .llm import LLMClient

logger = logging.getLogger(__name__)

# ── LLM system prompt ──────────────────────────────────────────

SYSTEM_PROMPT = """You are a crawl parameter generator. Given a natural-language description of what a user wants to crawl, you produce structured crawl parameters.

Respond ONLY with valid JSON matching this schema:
{
  "include_paths": [string] or null,
  "exclude_paths": [string] or null,
  "max_depth": int or null,
  "max_pages": int or null,
  "ignore_robots_txt": bool or null,
  "robots_user_agent": string or null,
  "deduplicate_similar_urls": bool or null,
  "reasoning": "Brief explanation of your choices"
}

Rules:
1. include_paths should use regex patterns like "blog/.*" or "products?/.*" or "docs/.*"
2. exclude_paths should use regex patterns like "admin/.*" or "login.*"
3. max_depth: 2 for most sites, 0 for just one page, 3+ for deep crawls
4. max_pages: 10 for "get everything important" on small sites, 50+ for large crawls, 1 for single page
5. ignore_robots_txt: true only if the user explicitly asks to ignore robots.txt
6. deduplicate_similar_urls: true for most crawls (removes query-param variants)
7. If the user mentions "blog" or "posts", include_paths should target "blog/.*" or "posts/.*"
8. If the user mentions "products" or "shop", include_paths should target "products?/.*" or "shop/.*"
9. If the user says "everything" or "all" or doesn't specify scope, leave include_paths as null
10. If the user says "except" or "but not" or "skip", put the excluded section in exclude_paths
11. If the user mentions "docs" or "documentation", include_paths should target "docs/.*"
12. Do NOT make up values. If the prompt doesn't suggest a value, set it to null.
13. Keep regex patterns simple and permissive — they will be applied as regex patterns.

Respond with ONLY the JSON object, no markdown, no code fences."""

# ── JSON schema for structured LLM output ──────────────────────

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "include_paths": {
            "type": "array",
            "items": {"type": "string"},
            "nullable": True,
        },
        "exclude_paths": {
            "type": "array",
            "items": {"type": "string"},
            "nullable": True,
        },
        "max_depth": {"type": "integer", "nullable": True},
        "max_pages": {"type": "integer", "nullable": True},
        "ignore_robots_txt": {"type": "boolean", "nullable": True},
        "robots_user_agent": {"type": "string", "nullable": True},
        "deduplicate_similar_urls": {"type": "boolean", "nullable": True},
        "reasoning": {"type": "string"},
    },
}


def _safe_parse_llm_response(text: str) -> dict[str, Any] | None:
    """Parse the LLM response text as JSON.

    Handles common formatting issues like markdown code fences,
    trailing commas, and leading/trailing whitespace.

    Returns the parsed dict, or ``None`` if parsing fails entirely.
    """
    cleaned = text.strip()

    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        # Find the first newline after opening fence
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            cleaned = cleaned[first_nl + 1 :]
        # Find closing fence
        fence_end = cleaned.rfind("```")
        if fence_end != -1:
            cleaned = cleaned[:fence_end].strip()

    # Try parsing as JSON
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to find a JSON-like object in the text
    try:
        brace_start = cleaned.index("{")
        brace_end = cleaned.rindex("}")
        if brace_end > brace_start:
            candidate = cleaned[brace_start : brace_end + 1]
            return json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        pass

    return None


def _validate_derived_params(
    params: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize the derived parameters.

    Handles type coercion and range validation. Returns a dict with
    only valid fields.
    """
    result: dict[str, Any] = {}

    # include_paths and exclude_paths: must be list of strings or None
    for field in ("include_paths", "exclude_paths"):
        value = params.get(field)
        if value is not None:
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                result[field] = value

    # max_depth: must be non-negative int or None
    md = params.get("max_depth")
    if md is not None:
        try:
            md_int = int(md)
            if md_int >= 0:
                result["max_depth"] = md_int
        except (ValueError, TypeError):
            pass

    # max_pages (limit): must be positive int or None
    mp = params.get("max_pages")
    if mp is not None:
        try:
            mp_int = int(mp)
            if mp_int >= 1:
                result["max_pages"] = mp_int
        except (ValueError, TypeError):
            pass

    # ignore_robots_txt: bool
    irt = params.get("ignore_robots_txt")
    if isinstance(irt, bool):
        result["ignore_robots_txt"] = irt

    # robots_user_agent: string
    rua = params.get("robots_user_agent")
    if isinstance(rua, str) and rua.strip():
        result["robots_user_agent"] = rua.strip()

    # deduplicate_similar_urls: bool
    dsu = params.get("deduplicate_similar_urls")
    if isinstance(dsu, bool):
        result["deduplicate_similar_urls"] = dsu

    return result


async def derive_crawl_params(
    prompt: str,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
) -> dict[str, Any]:
    """Derive crawl parameters from a natural-language prompt.

    Calls the LLM with a structured system prompt and JSON schema
    to extract crawl parameters from the user's description.

    Args:
        prompt: Natural-language description of what to crawl.
        llm_base_url: LLM API base URL.
        llm_api_key: LLM API key.
        llm_model: LLM model name.

    Returns:
        A dict with derived crawl parameters. Always returns valid
        fields (may be empty on failure). Includes an ``error`` key
        if the LLM call failed or returned invalid JSON.

        On success, returns fields like ``include_paths``,
        ``exclude_paths``, ``max_depth``, ``max_pages``, etc.
    """
    client = LLMClient(
        base_url=llm_base_url,
        api_key=llm_api_key,
        model=llm_model,
    )

    try:
        # First check if LLM is reachable
        if not await client.check_health():
            logger.warning("LLM backend unavailable for NL→params translation")
            return {
                "error": (
                    "LLM backend is not available. "
                    "Cannot derive crawl parameters from prompt. "
                    "Default parameters will be used."
                )
            }

        result_text = await client.generate(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"Crawl description: {prompt}\n\n"
                f"URL: <not needed for path derivation>\n\n"
                f"Generate crawl parameters for this description."
            ),
            schema=OUTPUT_SCHEMA,
        )

        # Check for error from LLM client
        if result_text.startswith("Error:"):
            logger.warning("LLM returned error for NL→params: %s", result_text)
            return {
                "error": (
                    f"LLM returned an error: {result_text}. "
                    "Default parameters will be used."
                )
            }

        # Parse the JSON response
        parsed = _safe_parse_llm_response(result_text)
        if parsed is None:
            logger.warning(
                "LLM returned unparseable JSON for NL→params: %s",
                result_text[:500],
            )
            return {
                "error": ("LLM returned invalid JSON. Default parameters will be used.")
            }

        # Validate and normalize
        result = _validate_derived_params(parsed)

        if not result:
            logger.warning(
                "LLM returned empty/no valid params for prompt: %s",
                prompt[:200],
            )
            return {
                "error": (
                    "LLM returned no usable parameters. "
                    "Default parameters will be used."
                )
            }

        logger.info(
            "NL→params derived: include_paths=%s, exclude_paths=%s, "
            "max_depth=%s, max_pages=%s from prompt=%s",
            result.get("include_paths"),
            result.get("exclude_paths"),
            result.get("max_depth"),
            result.get("max_pages"),
            prompt[:200],
        )
        return result

    except Exception as e:
        logger.error("NL→params LLM call failed: %s", e)
        return {"error": f"LLM call failed: {e}. Default parameters will be used."}
    finally:
        await client.close()


def merge_params(
    llm_derived: dict[str, Any],
    explicit: dict[str, Any],
) -> dict[str, Any]:
    """Merge LLM-derived params with explicit user params.

    Explicitly-set params override LLM-derived equivalents.
    Only the following fields are subject to merging:
    - ``include_paths``
    - ``exclude_paths``
    - ``max_depth`` (mapped from explicit ``max_depth`` in crawl request)
    - ``max_pages`` / ``limit`` (mapped from explicit ``max_pages``)

    Args:
        llm_derived: Params derived from the LLM (may include ``error``).
        explicit: Dict of explicitly-set params from the crawl request.
            Expected keys: ``include_paths``, ``exclude_paths``,
            ``max_depth``, ``max_pages``.

    Returns:
        Merged params dict. LLM-derived values that are overridden by
        explicit values are replaced. Explicit values that are ``None``
        don't override non-None LLM-derived values.
    """
    merged: dict[str, Any] = {}

    # Preserve error from LLM if present
    if "error" in llm_derived:
        merged["error"] = llm_derived["error"]

    # Fields where explicit overrides LLM (if explicit is not None)
    override_fields = [
        ("include_paths", "include_paths"),
        ("exclude_paths", "exclude_paths"),
        ("max_depth", "max_depth"),
        ("max_pages", "max_pages"),
    ]

    for llm_key, explicit_key in override_fields:
        llm_val = llm_derived.get(llm_key)
        explicit_val = explicit.get(explicit_key)

        if explicit_val is not None:
            # Explicit always wins
            merged[llm_key] = explicit_val
        elif llm_val is not None:
            merged[llm_key] = llm_val

    return merged
