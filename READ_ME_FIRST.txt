RaceOS backend — race endpoints + plan identity + map coordinates

INSTALL
    cd ~/development/race-os-backend
    unzip -o ~/Downloads/raceos-backend-races.zip
    rm READ_ME_FIRST.txt
    git add -A && git commit -m "Add races, plan identity, map coordinates" && git push

VERIFY
    grep -c "api/v1/races" backend/raceos/api/routers/races.py   # >= 1

WHAT THIS FIXES (the integration audit's blockers)
  G11  No endpoint created a Race, so a new signup could not start the plan
       builder at all. Now: POST/GET/PATCH/DELETE /api/v1/races.
  G17  PlanDetail carried no date, start time or course name, so the race
       card could not print its own heading. Now it does, plus ODbL
       attribution.
  G8   Route coordinates were unreachable from any public JSON endpoint.
       Now /courses/{ref}/recon returns them per leg, downsampled to 600
       points, GeoJSON order [lng, lat, elevation].

NOT FIXED — deliberate, answer these in the frontend session
  G21  warnings[] is still never emitted. Tell the session to skip it.
  G5   Courses have no date; dates live on a Race. Frontend removes the
       DATE / MONTH / countdown columns from the directory.
  G7   No course-GPX upload exists. Remove the promise.
  G14  IMPORTED is not a source. Use `manual`.

777 passed, 6 skipped. ruff and mypy --strict clean.
