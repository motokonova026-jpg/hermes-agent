import json
from pathlib import Path

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


def test_post_image_urls_include_carousel_children():
    from plugins.instagram_profile.client import _normalize_post

    post = _normalize_post(
        {
            "shortCode": "ABC123",
            "displayUrl": "https://cdn.example/cover.jpg",
            "childPosts": [
                {"displayUrl": "https://cdn.example/slide-1.jpg"},
                {"display_url": "https://cdn.example/slide-2.jpg"},
            ],
        }
    )

    assert post["image_urls"] == [
        "https://cdn.example/cover.jpg",
        "https://cdn.example/slide-1.jpg",
        "https://cdn.example/slide-2.jpg",
    ]


def test_download_public_post_images_caches_local_files(monkeypatch, tmp_path):
    from plugins.instagram_profile.client import download_post_images

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        content = b"fake-jpeg-bytes"

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, follow_redirects=True):
            assert url == "https://cdn.example/post.jpg"
            assert follow_redirects is True
            return FakeResponse()

    monkeypatch.setattr("plugins.instagram_profile.client.httpx.Client", FakeClient)
    posts = [{"url": "https://www.instagram.com/p/ABC123/", "image_urls": ["https://cdn.example/post.jpg"]}]

    enriched = download_post_images(posts, username="natgeo", cache_root=tmp_path, max_images=1)

    assert enriched[0]["local_image_paths"]
    saved = Path(enriched[0]["local_image_paths"][0])
    assert saved.exists()
    assert saved.read_bytes() == b"fake-jpeg-bytes"
    assert saved.suffix == ".jpg"
