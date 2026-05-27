"""Instagram profile viewer plugin.

Standalone plugin intended to be enabled for Willow. The tool stays visible even
without backend credentials so Willow can tell Bobby the exact setup step when
an Instagram profile link is sent.
"""

from __future__ import annotations

import logging

from plugins.instagram_profile.tools import INSTAGRAM_PROFILE_VIEW_SCHEMA, handle_instagram_profile_view

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    ctx.register_tool(
        name="instagram_profile_view",
        toolset="instagram_profile",
        schema=INSTAGRAM_PROFILE_VIEW_SCHEMA,
        handler=handle_instagram_profile_view,
        emoji="📸",
    )
    logger.debug("instagram_profile plugin loaded: instagram_profile_view registered")
