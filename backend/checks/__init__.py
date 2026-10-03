"""Importing this package triggers every ``@register`` decorator beneath it.

Add a new module to one of the two tuples below to bring its checks into the
registry — `_ALWAYS` if it reads the filesystem or the host, `_RADARCA` if it
talks to the HTTP display tier. A module in neither is never imported, which
test_profiles.py fails on rather than letting its checks go quiet.
"""
import importlib

from ..config import SETTINGS

# Modules that read the filesystem or the host, and are profile-independent.
# Imported on every profile. Two of these modules register MORE than their
# always-on checks: layer0_network also holds the radar-display TLS probe and
# layer0_episode the origin-episode correlation, both of which need a display
# tier. Those two registrations are gated on config.HAS_RADARCA inside their own
# modules, because the modules themselves cannot be gated -- layer0_episode
# exports attach_episode_suppression, which the wiring below calls regardless.
_ALWAYS = (
    "layer0_network",          # 2 control-ping checks (+1 radarca-gated)
    "layer0_selfhost",         # 2 self-monitoring checks (host disk, backup)
    "layer1_backend_product",  # LB1 (gated on SENTINEL_BACKEND_ROOT)
    "layer2_backend_radar",    # LB2 (gated)
    "layer0_episode",          # attach_episode_suppression (+1 radarca-gated)
)

# Modules that exist only because radarca does.
#
# === Skipped wholesale on a profile with no HTTP origin ===
#
# XQPI monitors FLOW straight off trinity; nothing serves it over HTTP. Every
# module below talks to SETTINGS.base, so on that profile they would register
# checks against an origin that does not exist — each one failing forever,
# each one paging, and L0 failures cascade-suppressing the backend checks that
# ARE working. Gating here rather than in each module keeps it to one place
# and one list, and the list lives in the profile so adding a radarca-only
# module later fails there rather than silently registering.
_RADARCA = (
    "layer0_latency",          # 1 latency canary — probes SETTINGS.base directly
    "layer0_website",          # 4 L0 checks
    "layer1_product",          # 13 product checks (L1 + L3B parity inline)
    "layer1_vector",           # 3 static-asset checks
    "layer1_stream",           # 1 stream-canary check
    "layer2_radar",            # 6 per-radar checks + 1 fleet correlation
    "layer3_overlay",          # 1 Playwright overlay parity check
    "layer4_image",            # 5 X-band + 3 mosaic image checks
)

if SETTINGS.profile == "xqpi":
    from ..profiles import xqpi as _profile_mod
    _skip = frozenset(_profile_mod.RADARCA_ONLY_MODULES)
    # The profile's list and this one must agree, or a module gets registered
    # against a nonexistent origin because two places drifted apart.
    assert _skip == frozenset(_RADARCA), (
        f"profile/registry disagree on radarca-only modules: "
        f"{_skip ^ frozenset(_RADARCA)}"
    )
else:
    _skip = frozenset()

for _name in (*_ALWAYS, *(m for m in _RADARCA if m not in _skip)):
    importlib.import_module(f".{_name}", __name__)

# Wire alarm suppression LAST: attach_episode_suppression walks the registry,
# so every check above must already be registered or it silently misses them.
from ..registry import CHECKS                      # noqa: E402
from .layer0_episode import attach_episode_suppression  # noqa: E402

_wired = attach_episode_suppression(CHECKS.values())
assert _wired, "episode suppression wired 0 checks — registry import order broke"

# future:
# from . import layer4_tier3_cross_radar
# from . import layer4_tier4_dualpol
# from . import layer4_tier5_classifier
