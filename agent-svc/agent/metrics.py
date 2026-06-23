"""Re-exports from common.metrics for backward compatibility.

All counter, histogram, and gauge primitives now live in ``common.metrics``.
This module re-exports the singleton and related types so existing
``from .metrics import METRICS`` imports continue to work.
"""

from common.metrics import (  # noqa: F401
    DEFAULT_BUCKETS,
    METRICS,
    MetricsCollector,
    _SafeCounter,
    _SafeGauge,
    _SafeHistogram,
)
