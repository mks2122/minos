"""Control tiers: L1 native tools, L2 typed adapters, L3 GUI fallback.

Preference order is always L1 -> L2 -> L3. The contract an adapter can declare
gets weaker as you descend -- L1 knows exactly which paths change, L3 only
observes -- which is why the router prefers the top and records every descent.
"""

from .base import Adapter, CapabilityManifest, OperationUnsupported, Preparation

__all__ = ["Adapter", "CapabilityManifest", "OperationUnsupported", "Preparation"]
