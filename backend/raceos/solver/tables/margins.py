"""Margin states, drift thresholds and lever significance. §A → ``margins.py``."""

from __future__ import annotations

from typing import Final

#: `margin_state` boundaries (§3.5). **Both are closed from above**: exactly
#: 20.0 is `clear`, exactly 0.0 is `tight`. Comparison is against the value
#: already rounded to 0.1 min, so a plan cannot flicker between states on a
#: float-representation difference.
MARGIN_CLEAR_MIN: Final[float] = 20.0
MARGIN_TIGHT_MIN: Final[float] = 0.0

#: A lever must move the reported barrier's ETA by at least this much to be
#: offered (§3.4). If none clears it, `lower_goal` is emitted alone — an honest
#: "nothing you can change before race day closes this gap".
LEVER_SIGNIFICANCE_MINUTES: Final[float] = 2.0

#: The one-at-a-time sensitivity perturbation, in the improving direction.
LEVER_PERTURBATION: Final[float] = 0.05

#: Drift detection (§A, and Build Spec Part 6.3). Duplicated into the settings
#: object as DRIFT_SPLIT_THRESHOLD_MINUTES / DRIFT_MARGIN_RISK_MINUTES, which
#: is what the *service* reads; these are the solver-side reference values.
DRIFT_SPLIT_THRESHOLD_MIN: Final[float] = 2.0
DRIFT_MARGIN_THRESHOLD_MIN: Final[float] = 20.0
