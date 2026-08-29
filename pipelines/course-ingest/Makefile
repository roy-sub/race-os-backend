CACHE ?= .cache
OUT   ?= out

.PHONY: install test generate regenerate validate visual-check golden clean

install:
	pip install -e ".[dev]"

test:
	pytest -q

# Regenerate every seeded course, its terrain extract and its visual check.
regenerate:
	course-ingest regenerate-all --cache $(CACHE) --out $(OUT)
	course-ingest visual-check --out $(OUT)

# Faster loop while iterating on a spec: no terrain extract.
generate:
	course-ingest generate $(SPEC) --cache $(CACHE) --out $(OUT) --skip-terrain

validate:
	course-ingest validate $(OUT)/bundles/*.bundle.json

visual-check:
	course-ingest visual-check --out $(OUT)

# Solver golden fixtures. Synthetic, static, and deliberately unrelated to the
# generated bundles -- see README.md, "Golden fixtures stay synthetic".
golden:
	python ../../backend/tests/golden/build_golden_courses.py

clean:
	rm -rf $(OUT)
