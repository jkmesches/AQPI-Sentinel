"""Single source of truth for the Sentinel version string.

Bump on every release tag — see docs/99-release-process.md. The
backend exposes this via /api/version so the frontend footer can
render it without baking a duplicate at frontend-build time.

Keep the format `MAJOR.MINOR.PATCH` (no leading `v`). The /api
endpoint and the frontend render add the `v` prefix for display.
"""

__version__ = "0.2.0"
