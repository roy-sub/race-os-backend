"""Regenerate the frozen golden expectations.

    python -m tests.golden.freeze            # all cases
    python -m tests.golden.freeze G07-HOT    # one case

**Changing a golden file requires an explicit reason in the commit message**,
and CI checks for one. That is not ceremony: the golden files are the only
artefact that can tell a deliberate model change from an accidental
regression, and a regeneration with no stated reason destroys that
distinction.
"""

from __future__ import annotations

import sys

from tests.golden.cases import CASES, CASES_BY_ID
from tests.golden.runner import write_expected


def main(argv: list[str]) -> int:
    selected = [CASES_BY_ID[name] for name in argv] if argv else list(CASES)
    for case in selected:
        path = write_expected(case)
        print(f"froze {case.case_id:20} -> {path.relative_to(path.parents[3])}")
    print(f"\n{len(selected)} case(s) frozen. State the reason in your commit message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
