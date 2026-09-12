# Incomplete tasks

## Partial — blocked on a fact this build does not have

- D2 — bike heat curve has no duration term. No published dose–response over a
  full-distance bike leg exists to derive a coefficient from. The model's range
  limits are surfaced as plan advisories instead; inventing a coefficient would
  put a number in the solver that no source supports.
- D3 — wetsuit thresholds. The update mechanism is built (`WetsuitRuleset`,
  with a `review_by` date and a test that fails once it passes). Only Ironman
  is populated: World Triathlon keys its table by swim distance as well as
  temperature and those values are not in hand. Transcribing them from memory
  is the exact failure the structure exists to prevent.

## Partial — needs a product decision before it is safe to build

- E1 — bike-split import. `ConstraintSource.IMPORTED` exists; the import path
  and the lock-bike-power solve mode do not. Locking bike power means exempting
  the bike leg from the barrier-adjustment loop that raises intensity to clear
  cut-offs — so a locked power that misses a cut-off must either fail the solve
  or silently override the athlete. That is a product decision, and it sits in
  the golden-file-gated part of the solver.

## Pending

- B3 — Race Mode offline: service worker, IndexedDB queue, wake lock, large-text
  mode, enlarged targets.
- B7 — accessibility pass to WCAG 2.1 AA.
- B8 — print parity with the PDF, legible in monochrome.
