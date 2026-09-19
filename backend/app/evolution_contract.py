"""Versioned, conservative release gates; never fitted to candidate results."""
from __future__ import annotations

import math

VERSION = "researcher-prompt-release-v2"
# A new version is required to change these predeclared quality gates.
MINIMUM_GAIN = 0.05


def proportion_interval(hits: int, total: int) -> tuple[float, float]:
    """Wilson 97.5% intervals (Bonferroni for a difference of two rates)."""
    if total < 1:
        return 0.0, 1.0
    z = 2.241402727604947
    p = hits / total
    scale = 1 + z * z / total
    center = (p + z * z / (2 * total)) / scale
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return max(0.0, center - radius), min(1.0, center + radius)


def gain_evidence(wins: int, losses: int, total: int) -> dict:
    lower = proportion_interval(wins, total)[0] - proportion_interval(losses, total)[1]
    upper = proportion_interval(wins, total)[1] - proportion_interval(losses, total)[0]
    return {"wins": wins, "losses": losses, "total": total,
            "gain": (wins - losses) / total if total else 0.0,
            "confidence_interval": [lower, upper], "confidence": 0.95,
            "minimum_gain": MINIMUM_GAIN,
            "passed": total >= 20 and lower >= MINIMUM_GAIN}
