"""API key authentication for semantic-svc migration endpoints.

Reads API_KEY from the environment. When configured, migration endpoints
require a valid API key via X-API-Key header or api_key query parameter.
"""

import logging
import os

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_KEY", "")


async def verify_api_key(request: Request) -> None:
    """Verify API key from X-API-Key header or api_key query parameter.

    If API_KEY is not configured (empty), all requests are allowed
    (insecure mode). When configured, requests without a valid key
    are rejected with HTTP 401.

    The VAL-FIX-003 contract specifies both X-API-Key header and
    api_key query parameter as supported authentication methods.
    """
    if not API_KEY:
        return

    # Check X-API-Key header
    x_api_key = request.headers.get("X-API-Key", "")
    if x_api_key == API_KEY:
        return

    # Check api_key query parameter
    query_api_key = request.query_params.get("api_key", "")
    if query_api_key == API_KEY:
        return

    raise HTTPException(status_code=401, detail="Invalid or missing API key.")
