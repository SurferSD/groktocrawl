"""Unit tests for common/features.py — the env-var-based feature toggle system.

Covers all truthy, falsy, unset, and malformed values, as well as
case-insensitivity and the guarantee that ``is_enabled`` always returns
a ``bool`` (never ``None``).
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from common.features import is_enabled

# ---------------------------------------------------------------------------
# Truthy values — is_enabled should return True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value",),
    [
        ("true",),
        ("True",),
        ("TRUE",),
        ("tRuE",),  # arbitrary casing
        ("1",),
    ],
)
def test_truthy_values(value: str) -> None:
    """is_enabled returns True for all recognised truthy strings."""
    with mock.patch.dict(os.environ, {"FEATURE_MYFLAG": value}, clear=False):
        assert is_enabled("myflag") is True


# ---------------------------------------------------------------------------
# Falsy values — is_enabled should return False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value",),
    [
        ("false",),
        ("False",),
        ("FALSE",),
        ("fAlSe",),  # arbitrary casing
        ("0",),
        ("",),  # empty string
    ],
)
def test_falsy_values(value: str) -> None:
    """is_enabled returns False for all recognised falsy strings."""
    with mock.patch.dict(os.environ, {"FEATURE_MYFLAG": value}, clear=False):
        assert is_enabled("myflag") is False


# ---------------------------------------------------------------------------
# Unset environment variable
# ---------------------------------------------------------------------------


def test_unset_env_var() -> None:
    """is_enabled returns False when the FEATURE_ variable is not set."""
    # Ensure the variable is absent
    if "FEATURE_MYFLAG" in os.environ:
        del os.environ["FEATURE_MYFLAG"]
    assert is_enabled("myflag") is False


# ---------------------------------------------------------------------------
# Malformed / unexpected values — is_enabled should return False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value",),
    [
        ("maybe",),
        ("yes",),
        ("no",),
        ("enabled",),
        ("disabled",),
        ("on",),
        ("off",),
        ("2",),
        ("-1",),
        ("  true  ",),  # whitespace is stripped -> "true" -> truthy
    ],
)
def test_malformed_values(value: str) -> None:
    """is_enabled returns False for values that are not truthy."""
    with mock.patch.dict(os.environ, {"FEATURE_MALFORMED": value}, clear=False):
        result = is_enabled("malformed")
        # Note: whitespace-trimmed "  true  " becomes "true" which IS truthy
        expected = value.strip().lower() in {"true", "1"}
        assert result is expected, f"Expected {expected!r} for value {value!r}"


# ---------------------------------------------------------------------------
# Case-insensitivity of feature names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("feature_input",),
    [
        ("X",),
        ("x",),
        ("FLAG",),
        ("flag",),
        ("Flag",),
        ("MY_FEATURE",),
        ("my_feature",),
        ("My_Feature",),
    ],
)
def test_case_insensitive_feature_names(feature_input: str) -> None:
    """Feature names are case-insensitive — they all read the same env var."""
    with mock.patch.dict(
        os.environ, {"FEATURE_X": "true", "FEATURE_FLAG": "false"}, clear=False
    ):
        # For features like "X"/"x" we know FEATURE_X=true
        # For features like "FLAG"/"flag" we know FEATURE_FLAG=false
        if feature_input.upper() == "X":
            assert is_enabled(feature_input) is True
        else:
            assert is_enabled(feature_input) is False


# ---------------------------------------------------------------------------
# Guarantee: always returns bool, never None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_val",),
    [
        ("true",),
        ("false",),
        (None,),  # unset
        ("maybe",),  # malformed
        ("",),  # empty
        ("1",),
        ("0",),
    ],
)
def test_always_returns_bool(env_val: str | None) -> None:
    """is_enabled always returns a bool, never None."""
    if env_val is None:
        # Unset
        if "FEATURE_BOOLCHECK" in os.environ:
            del os.environ["FEATURE_BOOLCHECK"]
    else:
        os.environ["FEATURE_BOOLCHECK"] = env_val
    result = is_enabled("boolcheck")
    assert isinstance(result, bool), f"Expected bool, got {type(result).__name__}"
