"""Agent-facing Instagram profile viewer tool."""

from __future__ import annotations

import json
from typing import Any, Dict

from plugins.instagram_profile.client import (
    InstagramProfileError,
    InstagramProfileSetupError,
    normalize_instagram_profile,
    view_instagram_profile,
)
from tools.registry import tool_result


INSTAGRAM_PROFILE_VIEW_SCHEMA: Dict[str, Any] = {
    "name": "instagram_profile_view",
    "description": (
        "View and summarize a public Instagram profile from an instagram.com profile URL or @handle. "
        "Trigger: when Bobby sends Willow an Instagram profile link like `https://www.instagram.com/<username>/` "
        "or asks about an Instagram @handle, call this tool before answering. Public profiles only; do not claim "
        "access to private/login-only content. If backend setup is missing, surface the setup hint exactly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "description": "Instagram profile URL or @handle, e.g. https://www.instagram.com/natgeo/ or @natgeo.",
            },
            "max_posts": {
                "type": "integer",
                "description": "Maximum recent posts to include in the structured result. Default 12; capped at 24.",
                "default": 12,
                "minimum": 0,
                "maximum": 24,
            },
            "download_images": {
                "type": "boolean",
                "description": "Download public post images/thumbnails to Willow's local cache for visual inspection. Default true.",
                "default": True,
            },
            "max_images": {
                "type": "integer",
                "description": "Maximum number of public post images to cache locally. Default 6; capped at 24.",
                "default": 6,
                "minimum": 0,
                "maximum": 24,
            },
        },
        "required": ["profile"],
        "additionalProperties": False,
    },
}


def _struct_error(error_key: str, hint: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"error": error_key, "hint": hint}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return json.dumps(payload, ensure_ascii=False)


def handle_instagram_profile_view(args: dict, **kw: Any) -> str:
    profile = str(args.get("profile") or "").strip()
    try:
        # Normalize first so setup errors can still include a canonical URL.
        normalized = normalize_instagram_profile(profile)
        result = view_instagram_profile(
            profile,
            max_posts=args.get("max_posts", 12),
            download_images=args.get("download_images", True) is not False,
            max_images=args.get("max_images", 6),
        )
        return tool_result(result)
    except ValueError as exc:
        return _struct_error("instagram_profile_invalid_input", str(exc))
    except InstagramProfileSetupError as exc:
        normalized_url = None
        username = None
        try:
            normalized = normalize_instagram_profile(profile)
            normalized_url = normalized.profile_url
            username = normalized.username
        except Exception:
            pass
        return _struct_error(
            "instagram_profile_setup_required",
            str(exc),
            setup_step="apify_token",
            profile_url=normalized_url,
            username=username,
        )
    except InstagramProfileError as exc:
        return _struct_error("instagram_profile_fetch_failed", str(exc))
    except Exception as exc:
        return _struct_error("instagram_profile_unexpected_error", f"Instagram profile viewer failed: {type(exc).__name__}: {exc}")
