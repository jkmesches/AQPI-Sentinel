"""Importing this package triggers every ``@register`` decorator beneath it.

Add a new module to `_ALWAYS` below if it reads the filesystem or the host, or
to `config.RADARCA_ONLY_MODULES` if it talks to the HTTP display tier. A module
in neither is never imported, which test_profiles.py fails on rather than
letting its checks go quiet.
"""
import importlib

from ..config import HAS_RADARCA, RADARCA_ONLY_MODULES, SETTINGS

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
    "layer2_backend_radar",    # LB2 (gated) + the fleet correlation check
    "layer3_backend_processing",  # LB3 (gated)
    "layer0_episode",          # attach_episode_suppression (+1 radarca-gated)
)

# The radarca-only module list is config.RADARCA_ONLY_MODULES — one canonical
# copy, because registry.register() needs it too (it refuses a check from one
# of these modules when HAS_RADARCA is false, which is the guarantee; skipping
# the import here is only an optimisation on top of it).
#
# Skipped wholesale on a profile with no HTTP origin: XQPI monitors FLOW
# straight off trinity and nothing serves it over HTTP, so every module below
# would register checks against an origin that does not exist — each failing
# forever, each paging, and L0 failures cascade-suppressing the backend checks
# that ARE working.
_RADARCA = RADARCA_ONLY_MODULES

for _name in (*_ALWAYS, *(_RADARCA if HAS_RADARCA else ())):
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
