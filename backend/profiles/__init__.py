"""Deployment profiles — which radars and products a Sentinel instance watches.

Selected by ``SENTINEL_PROFILE``. Absent or "aqpi" keeps the tables defined in
``config.py``; "xqpi" swaps them for the FLOW radar on trinity.

One image, two profiles, which is the pattern ``shirejoe`` and ``cira-aqpi``
already use for the backend mounts — a fork would have to be kept in sync by
hand across every check, label and threshold.

Profile modules are PURE DATA and must not import ``config``: ``config``
imports them, so the dependency only runs one way.
"""
