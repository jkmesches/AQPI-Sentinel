"""Importing this package triggers every ``@register`` decorator beneath it.

Add a new module here to bring its checks into the registry.
"""
from . import layer0_network   # noqa: F401  — 1 control-ping check (internet up?)
from . import layer0_selfhost  # noqa: F401  — 1 self-monitoring check (host disk)
from . import layer0_website   # noqa: F401  — 4 L0 checks
from . import layer1_product   # noqa: F401  — 13 product checks (L1 + L3B parity inline)
from . import layer1_vector    # noqa: F401  — 3 static-asset checks
from . import layer1_stream    # noqa: F401  — 1 stream-canary check
from . import layer2_radar     # noqa: F401  — 6 per-radar checks + 1 fleet correlation
from . import layer3_overlay   # noqa: F401  — 1 Playwright overlay parity check
from . import layer4_image     # noqa: F401  — 5 X-band + 3 mosaic image checks
# future:
# from . import layer4_tier3_cross_radar
# from . import layer4_tier4_dualpol
# from . import layer4_tier5_classifier
