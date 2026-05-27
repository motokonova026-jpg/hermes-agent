import json

from plugins.instagram_profile.client import normalize_instagram_profile
from plugins.instagram_profile.tools import handle_instagram_profile_view


def test_normalize_instagram_profile_url_and_handle():
    assert normalize_instagram_profile("https://www.instagram.com/natgeo/").username == "natgeo"
    assert normalize_instagram_profile("@natgeo").profile_url == "https://www.instagram.com/natgeo/"


def test_handler_returns_setup_required_without_token(monkeypatch):
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
    monkeypatch.delenv("INSTAGRAM_PROFILE_APIFY_TOKEN", raising=False)
    payload = json.loads(handle_instagram_profile_view({"profile": "https://www.instagram.com/natgeo/", "max_posts": 3}))
    assert payload["error"] == "instagram_profile_setup_required"
    assert payload["setup_step"] == "apify_token"
    assert payload["username"] == "natgeo"


def test_handler_rejects_content_paths():
    payload = json.loads(handle_instagram_profile_view({"profile": "https://www.instagram.com/p/abc123/"}))
    assert payload["error"] == "instagram_profile_invalid_input"
