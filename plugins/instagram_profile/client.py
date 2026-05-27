"""Instagram public profile viewer client.

Default backend is Apify because direct Instagram scraping from a VPS is
fragile. The tool intentionally handles public-profile research only; it does
not accept or store Instagram login credentials.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import httpx

from hermes_constants import get_hermes_home


DEFAULT_ACTOR_ID = "apify/instagram-profile-scraper"
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
URL_RE = re.compile(r"https?://(?:www\.|m\.)?instagram\.com/([^/?#]+)/?", re.I)
RESERVED_PATHS = {
    "about",
    "accounts",
    "api",
    "developer",
    "direct",
    "explore",
    "oauth",
    "p",
    "privacy",
    "reel",
    "reels",
    "stories",
    "terms",
}


class InstagramProfileError(Exception):
    """Base Instagram profile viewer error."""


class InstagramProfileSetupError(InstagramProfileError):
    """Raised when backend credentials/config are missing."""


@dataclass(frozen=True)
class InstagramProfileRequest:
    username: str
    profile_url: str


def normalize_instagram_profile(value: str) -> InstagramProfileRequest:
    """Return canonical username/profile URL from an Instagram URL or @handle."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("instagram profile URL or @handle is required")

    # Prefer explicit Instagram URL extraction even if extra text surrounds it.
    match = URL_RE.search(raw)
    if match:
        username = match.group(1).strip("/").split("/")[0]
    else:
        candidate = raw.strip()
        if candidate.startswith("@"):
            candidate = candidate[1:]
        if candidate.lower().startswith("instagram.com/"):
            candidate = candidate.split("/", 1)[1]
        username = candidate.strip("/").split("/")[0]

    username = username.strip().lstrip("@")
    username = username.split("?")[0].split("#")[0]
    if not USERNAME_RE.match(username):
        raise ValueError("Instagram username must be 1-30 chars: letters, numbers, dot, underscore")
    if username.lower() in RESERVED_PATHS:
        raise ValueError(f"'{username}' is an Instagram content path, not a profile username")
    return InstagramProfileRequest(
        username=username,
        profile_url=f"https://www.instagram.com/{username}/",
    )


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _first_str(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_bool(*values: Any) -> Optional[bool]:
    for value in values:
        if isinstance(value, bool):
            return value
    return None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _extract_count(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _dedupe_strings(values: Iterable[Any]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _post_image_urls(raw: Mapping[str, Any]) -> List[str]:
    """Collect direct image/display URLs from a post, including carousels."""
    urls: List[Any] = [
        raw.get("displayUrl"),
        raw.get("display_url"),
        raw.get("thumbnailUrl"),
        raw.get("thumbnail_url"),
        raw.get("imageUrl"),
        raw.get("image_url"),
    ]
    for key in ("childPosts", "child_posts", "children", "sidecarChildren", "carouselMedia", "carousel_media"):
        for child in _as_list(raw.get(key)):
            if isinstance(child, Mapping):
                urls.extend(
                    [
                        child.get("displayUrl"),
                        child.get("display_url"),
                        child.get("thumbnailUrl"),
                        child.get("thumbnail_url"),
                        child.get("imageUrl"),
                        child.get("image_url"),
                    ]
                )
    return _dedupe_strings(urls)


def _normalize_post(raw: Mapping[str, Any]) -> Dict[str, Any]:
    shortcode = _first_str(raw.get("shortCode"), raw.get("shortcode"), raw.get("code"))
    url = _first_str(raw.get("url"), raw.get("link"))
    if not url and shortcode:
        url = f"https://www.instagram.com/p/{shortcode}/"
    image_urls = _post_image_urls(raw)
    return {
        "url": url,
        "caption": _first_str(raw.get("caption"), raw.get("text"), raw.get("description")),
        "timestamp": _first_str(raw.get("timestamp"), raw.get("date"), raw.get("takenAt"), raw.get("taken_at")),
        "media_type": _first_str(raw.get("type"), raw.get("mediaType"), raw.get("media_type")),
        "thumbnail_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
        "local_image_paths": [],
        "likes": _extract_count(raw, "likesCount", "likes_count", "likes"),
        "comments": _extract_count(raw, "commentsCount", "comments_count", "comments"),
    }


def _extension_for_response(response: httpx.Response, url: str) -> str:
    content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def _default_cache_root() -> Path:
    return get_hermes_home() / "cache"


def download_post_images(
    posts: List[Dict[str, Any]],
    *,
    username: str,
    cache_root: Path | str | None = None,
    max_images: int = 6,
    timeout: float = 30.0,
) -> List[Dict[str, Any]]:
    """Download public post images and add local paths for downstream vision use."""
    limit = _coerce_int(max_images, default=6, minimum=0, maximum=24)
    enriched = [dict(post) for post in posts]
    if limit <= 0:
        return enriched
    root = Path(cache_root) if cache_root is not None else _default_cache_root()
    target_dir = root / "instagram_profile" / username
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    with httpx.Client(timeout=timeout) as client:
        for post_index, post in enumerate(enriched, start=1):
            local_paths: List[str] = []
            failures: List[str] = []
            for image_url in _dedupe_strings(post.get("image_urls") or [post.get("thumbnail_url")]):
                if downloaded >= limit:
                    break
                try:
                    response = client.get(image_url, follow_redirects=True)
                    response.raise_for_status()
                    content_type = str(response.headers.get("content-type") or "").lower()
                    if "image/" not in content_type:
                        failures.append(f"non_image:{image_url}")
                        continue
                    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:16]
                    ext = _extension_for_response(response, image_url)
                    path = target_dir / f"{post_index:02d}_{digest}{ext}"
                    path.write_bytes(response.content)
                    local_paths.append(str(path))
                    downloaded += 1
                except Exception as exc:
                    failures.append(f"{type(exc).__name__}:{image_url}")
            post["local_image_paths"] = local_paths
            if failures:
                post["image_download_errors"] = failures[:3]
            if downloaded >= limit:
                break
    return enriched


def _collect_posts(item: Mapping[str, Any], limit: int) -> List[Dict[str, Any]]:
    candidates: List[Any] = []
    for key in ("latestPosts", "latest_posts", "posts", "edge_owner_to_timeline_media", "items"):
        value = item.get(key)
        if isinstance(value, Mapping) and isinstance(value.get("edges"), list):
            candidates.extend(edge.get("node", edge) for edge in value.get("edges", []))
        else:
            candidates.extend(_as_list(value))
    posts: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, Mapping):
            continue
        post = _normalize_post(raw)
        identity = post.get("url") or post.get("caption") or json.dumps(raw, sort_keys=True, default=str)[:80]
        if identity in seen:
            continue
        seen.add(identity)
        posts.append(post)
        if len(posts) >= limit:
            break
    return posts


def _normalize_profile_item(item: Mapping[str, Any], request: InstagramProfileRequest, max_posts: int) -> Dict[str, Any]:
    username = _first_str(item.get("username"), item.get("userName"), item.get("ownerUsername"), request.username)
    private = _first_bool(item.get("private"), item.get("isPrivate"), item.get("is_private"))
    verified = _first_bool(item.get("verified"), item.get("isVerified"), item.get("is_verified"))
    profile_url = _first_str(item.get("url"), item.get("profileUrl"), request.profile_url)
    return {
        "success": True,
        "source": "apify",
        "profile_url": profile_url,
        "username": username or request.username,
        "display_name": _first_str(item.get("fullName"), item.get("full_name"), item.get("name")),
        "bio": _first_str(item.get("biography"), item.get("bio"), item.get("description")),
        "external_url": _first_str(item.get("externalUrl"), item.get("external_url"), item.get("website")),
        "profile_pic_url": _first_str(item.get("profilePicUrl"), item.get("profile_pic_url"), item.get("profilePictureUrl")),
        "is_private": private,
        "is_verified": verified,
        "followers": _extract_count(item, "followersCount", "followers_count", "followers"),
        "following": _extract_count(item, "followsCount", "followingCount", "following_count", "following"),
        "post_count": _extract_count(item, "postsCount", "posts_count", "mediaCount", "media_count"),
        "recent_posts": _collect_posts(item, max_posts),
        "note": "Public-profile metadata only. Private or login-only content is not accessible through this tool.",
    }


def _apify_input(request: InstagramProfileRequest, max_posts: int) -> Dict[str, Any]:
    # Most Instagram profile actors accept one of these common shapes. Keep the
    # default conservative and allow overriding entirely for a different actor.
    custom = _env("INSTAGRAM_PROFILE_APIFY_INPUT_JSON")
    if custom:
        payload = json.loads(custom)
        text = json.dumps(payload)
        text = text.replace("{{username}}", request.username).replace("{{profile_url}}", request.profile_url).replace("{{max_posts}}", str(max_posts))
        return json.loads(text)
    return {
        "usernames": [request.username],
        "resultsLimit": max_posts,
        "resultsType": "posts",
        "searchLimit": 1,
    }


def fetch_with_apify(request: InstagramProfileRequest, *, max_posts: int, timeout: float = 90.0) -> Dict[str, Any]:
    token = _env("APIFY_API_TOKEN") or _env("INSTAGRAM_PROFILE_APIFY_TOKEN")
    if not token:
        raise InstagramProfileSetupError(
            "Set APIFY_API_TOKEN or INSTAGRAM_PROFILE_APIFY_TOKEN in Willow's protected .env to enable Instagram profile viewing."
        )
    actor_id = _env("INSTAGRAM_PROFILE_APIFY_ACTOR_ID", DEFAULT_ACTOR_ID)
    encoded_actor = urllib.parse.quote(actor_id, safe="")
    url = f"https://api.apify.com/v2/acts/{encoded_actor}/run-sync-get-dataset-items"
    payload = _apify_input(request, max_posts)
    params = {"token": token, "clean": "true", "format": "json"}
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, params=params, json=payload)
    if response.status_code in {401, 403}:
        raise InstagramProfileSetupError("Apify rejected the token or actor access for Instagram profile viewing.")
    if response.status_code >= 400:
        raise InstagramProfileError(f"Apify Instagram actor failed: HTTP {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except Exception as exc:
        raise InstagramProfileError(f"Apify returned non-JSON output: {exc}") from exc
    items = data if isinstance(data, list) else _as_list(data)
    item = next((entry for entry in items if isinstance(entry, Mapping)), None)
    if not item:
        return {
            "success": False,
            "source": "apify",
            "profile_url": request.profile_url,
            "username": request.username,
            "error": "profile_not_found_or_unavailable",
            "hint": "The profile may be private, missing, blocked, or the configured scraper actor returned no items.",
        }
    result = _normalize_profile_item(item, request, max_posts)
    result["retrieved_at_unix"] = int(time.time())
    result["apify_actor_id"] = actor_id
    return result


def view_instagram_profile(
    value: str,
    *,
    max_posts: int = 12,
    download_images: bool = True,
    max_images: int = 6,
) -> Dict[str, Any]:
    request = normalize_instagram_profile(value)
    limit = _coerce_int(max_posts, default=12, minimum=0, maximum=24)
    result = fetch_with_apify(request, max_posts=limit)
    if result.get("success") and download_images:
        image_limit = _coerce_int(max_images, default=6, minimum=0, maximum=24)
        posts = result.get("recent_posts")
        if isinstance(posts, list):
            result["recent_posts"] = download_post_images(posts, username=request.username, max_images=image_limit)
            result["media_download_summary"] = {
                "requested": True,
                "max_images": image_limit,
                "downloaded": sum(len(post.get("local_image_paths") or []) for post in result["recent_posts"] if isinstance(post, Mapping)),
                "cache_root": str(_default_cache_root() / "instagram_profile" / request.username),
            }
    elif result.get("success"):
        result["media_download_summary"] = {"requested": False}
    return result
