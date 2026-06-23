"""Webhook delivery for async job endpoints.

Called from worker functions after store.complete_job() / store.fail_job().
Retries with exponential backoff, optional HMAC signing.
Each event includes a unique UUID v4 ``webhookId`` for receiver-side
deduplication (VAL-PARITY-011).

Payload format (per validation contract):
    - ``type`` — the event type (e.g. ``"crawl.started"``)
    - ``id`` — the job ID
    - ``webhookId`` — unique UUID v4 per delivery
    - ``success`` — boolean, ``True`` for normal events
    - ``error`` — ``None`` for success, error string for failures
    - ``data`` — list for crawl events (``[]`` or ``[{...}]``),
                 dict for other job types
    - ``metadata`` — echo of ``webhook.metadata`` (VAL-PARITY-009)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import uuid

import httpx

from .settings import load_settings

logger = logging.getLogger(__name__)

_webhook_settings = load_settings()
MAX_RETRIES = 3
TIMEOUT_SECONDS = 5


def _next_webhook_id() -> str:
    """Generate a unique UUID v4 for webhookId (VAL-PARITY-011).

    Each call returns a new UUID v4 string, ensuring uniqueness
    across all deliveries regardless of job or event type.

    Returns:
        A UUID v4 string like ``"f47ac10b-58cc-4372-a567-0e02b2c3d479"``.
    """
    return str(uuid.uuid4())


def _sign_body(body: bytes, secret: str) -> str:
    """HMAC-SHA256 sign the request body."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver_webhook(
    webhook_config: dict | None,
    event: str,
    job_id: str,
    data: dict | list | None = None,
    task_tracker: object = None,
    success: bool = True,
    error: str | None = None,
) -> None:
    """POST a job event to the configured webhook URL with retry logic.

    Each delivery includes a unique UUID v4 ``webhookId`` field for
    receiver-side deduplication (VAL-PARITY-011).

    Payload format:
        ``{"type": event, "id": job_id, "webhookId": "<uuid>",
          "success": success, "error": error, "data": data,
          "metadata": {metadata_echo}}``

    Args:
        webhook_config: Dict with 'url', optional 'events' list, and
                        optional 'metadata' dict (echoed in every event).
                        Example: ``{"url": "https://example.com/hook",
                                   "events": ["crawl.completed"],
                                   "metadata": {"customer_id": "123"}}``
        event: The event type (e.g. ``"completed"``, ``"failed"``,
               ``"crawl.started"``, ``"crawl.page"``).
        job_id: The job identifier.
        data: Payload to include in the body. For crawl lifecycle events
              (started, completed, failed), pass ``[]``. For per-page
              events, pass ``[{url, markdown, ...}]``.
        task_tracker: Optional ``TaskTracker`` instance. If provided, the
                      webhook delivery is spawned as a tracked background
                      task instead of executing inline.
        success: Whether the event indicates success (default ``True``).
        error: Error message for failure events (default ``None``).
    """
    if not webhook_config:
        return

    url = webhook_config.get("url")
    if not url:
        return

    # Respect events filter — if events list provided, only fire for matching events
    events_filter = webhook_config.get("events")
    if events_filter and event not in events_filter:
        return

    webhook_id = _next_webhook_id()

    payload = {
        "type": event,
        "id": job_id,
        "webhookId": webhook_id,
        "success": success,
        "error": error,
        "data": data or ([] if data is None else data),
        "metadata": webhook_config.get("metadata", {}) if webhook_config else {},
    }
    body = json.dumps(payload).encode()

    headers = {"Content-Type": "application/json"}
    if _webhook_settings.webhook_secret:
        headers["X-Webhook-Signature"] = (
            f"sha256={_sign_body(body, _webhook_settings.webhook_secret)}"
        )

    async def _do_deliver() -> None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                    resp = await client.post(url, content=body, headers=headers)
                    if resp.status_code < 500:
                        logger.info(
                            "Webhook delivered %s for job %s (webhookId=%s, status %d)",
                            event,
                            job_id,
                            webhook_id,
                            resp.status_code,
                        )
                        return
                    logger.warning(
                        "Webhook attempt %d/%d returned %d for job %s (webhookId=%s)",
                        attempt,
                        MAX_RETRIES,
                        resp.status_code,
                        job_id,
                        webhook_id,
                    )
            except httpx.TimeoutException:
                logger.warning(
                    "Webhook attempt %d/%d timed out for job %s (webhookId=%s)",
                    attempt,
                    MAX_RETRIES,
                    job_id,
                    webhook_id,
                )
            except Exception as e:
                logger.warning(
                    "Webhook attempt %d/%d failed for job %s: %s (webhookId=%s)",
                    attempt,
                    MAX_RETRIES,
                    job_id,
                    e,
                    webhook_id,
                )

            if attempt < MAX_RETRIES:
                await asyncio.sleep(2**attempt)  # 2s, 4s

        logger.error(
            "Webhook delivery failed for job %s after %d attempts (webhookId=%s)",
            job_id,
            MAX_RETRIES,
            webhook_id,
        )

    if task_tracker is not None and hasattr(task_tracker, "create_background_task"):
        task_tracker.create_background_task(_do_deliver())
    else:
        await _do_deliver()
