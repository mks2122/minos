"""Verified skills.

A skill here is not a blob of remembered steps. It carries its own capability
manifest and its own effect oracles, so replay is scoped, re-verified, and
abandoned when the world has moved. Irreversible steps re-prompt every time:
caching skips the thinking, not the consent.
"""

from .promote import PromotionRefused, promote, why_not_promotable
from .replay import DriftDetected, SkillPlanner, check_drift, replay_is_safe
from .skill import Binding, Skill, SkillStep
from .store import SkillStore

__all__ = [
    "Binding",
    "DriftDetected",
    "PromotionRefused",
    "Skill",
    "SkillPlanner",
    "SkillStep",
    "SkillStore",
    "check_drift",
    "promote",
    "replay_is_safe",
    "why_not_promotable",
]
