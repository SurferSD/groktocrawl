"""Environment-variable-based feature toggle system.

Exposes ``is_enabled()`` to check whether a named feature flag is active.
Feature flags are read from environment variables with the ``FEATURE_`` prefix.
All feature toggles default to OFF (``False``).

Feature names are case-insensitive: ``is_enabled("X")`` and ``is_enabled("x")``
both read ``FEATURE_X``.

Truthy values (return ``True``):
    ``true``, ``True``, ``TRUE``, ``1``

Falsy values (return ``False``):
    Unset, empty string, ``false``, ``False``, ``FALSE``, ``0``, or any other value.
"""

import os

_TRUTHY = frozenset({"true", "1"})


def is_enabled(feature_name: str) -> bool:
    """Check whether a feature toggle is enabled.

    Args:
        feature_name: The feature name (case-insensitive).  The corresponding
            environment variable is ``FEATURE_`` + ``feature_name.upper()``.

    Returns:
        ``True`` if the environment variable is set to a truthy value,
        ``False`` otherwise (including unset, empty, falsy, or malformed).
    """
    env_var = f"FEATURE_{feature_name.upper()}"
    value = os.environ.get(env_var, "").strip()
    return value.lower() in _TRUTHY
