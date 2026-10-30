# Applying 0.1.5 (patch series against the 0.1.4 wheel baseline)

1. In the real repo (at the 0.1.4 tag or at cf9cd36, which only changed README/landing), run
   `git am /path/patches/*.patch`. The paths are `src/agentcap/...` and `tests/...`.
   If the repo layout differs, use `git am -p1 --directory=<dir>` or `git apply --3way`.
2. Bump the version to 0.1.5 in pyproject.toml (the wheel has none, so the patch only changes
   `__init__.py`). Check that `data/pricing.json` is included as package data.
3. The new tests live in `tests/test_anthropic_*.py`, `tests/test_claude_adapter_and_tiers.py`
   and `tests/test_e2e_claude.py`, with a shared `tests/conftest.py`. If the repo already has a
   conftest.py, rename it to `tests/anthropic_harness.py` and fix the imports (`from conftest import body`).
   pytest.ini sets `asyncio_mode = auto`.
4. Existing tests that may need updating because behaviour changed on purpose:
   - Tests asserting that `--hour-cap`/`--day-cap` exit 2 on Free, or the content of `FREE_FEATURES`.
     Caps are now Free.
   - Tests checking the exact event list for repeated identical requests on Free.
     A `loop_nudge` event is now emitted once.
   - Tests on the exact `child_env()` keys (`ANTHROPIC_BASE_URL` was added) or on the exact
     ZERO_REQUEST_HINT, UNSUPPORTED_CLI_HINT and PRO_HINT text.
   - Tests on the `pricing.json` model count (17 anthropic/* entries added) or on `ALLOWED_FIELDS`
     (`requests` and `exit_code` added).
5. Copy the copy/*.md and LANDING_copy.txt text into the README and landing page. Keep
   landing/deploy/ and landing/README.md out of git.
