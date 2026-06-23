"""pytest configuration for GroktoCrawl unit tests.

Adds the project root and agent-svc to the Python path so that unit tests
can import the agent modules directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path (for common.* imports)
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Add agent-svc to path (for agent.* imports)
_agent_svc = _root / "agent-svc"
if _agent_svc.exists() and str(_agent_svc) not in sys.path:
    sys.path.insert(0, str(_agent_svc))
