"""Venice image generation backend.

Uses Venice's native ``POST /api/v1/image/generate`` endpoint and returns the
base64 image(s) as local cached files. This is intentionally separate from the
chat model provider: Willow can keep Grok for chat while routing the generic
``image_generate`` tool to Venice.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "venice-sd35"
DEFAULT_FORMAT = "webp"
DEFAULT_SAFE_MODE = False
DEFAULT_CFG_SCALE = 7.5
DEFAULT_STEPS = 8

_MODELS: Dict[str, Dict[str, Any]] = {
    "venice-sd35": {
        "display": "Venice SD35",
        "speed": "~5-15s",
        "strengths": "Venice default, balanced creative image generation",
    },
    "flux-2-pro": {
        "display": "Flux 2 Pro",
        "speed": "~10-30s",
        "strengths": "High-quality prompt adherence",
    },
    "hidream": {
        "display": "HiDream",
        "speed": "~10-30s",
        "strengths": "Aesthetic general image generation",
    },
    "qwen-image-2-pro": {
        "display": "Qwen Image 2 Pro",
        "speed": "~10-30s",
        "strengths": "Strong text/rendering and composition",
    },
}

_DIMENSIONS: Dict[str, Tuple[int, int]] = {
    "landscape": (1280, 720),
    "square": (1024, 1024),
    "portrait": (720, 1280),
}

_ASPECT_RATIOS: Dict[str, str] = {
    "landscape": "16:9",
    "square": "1:1",
    "portrait": "9:16",
}

_FORMAT_EXTENSIONS = {"jpeg": "jpg", "png": "png", "webp": "webp"}


def _load_venice_config() -> Dict[str, Any]:
    """Read ``image_gen.venice`` from config.yaml."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        venice_section = section.get("venice") if isinstance(section, dict) else None
        return venice_section if isinstance(venice_section, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive logging only
        logger.debug("Could not load image_gen.venice config: %s", exc)
        return {}


def _cfg_bool(name: str, default: bool) -> bool:
    value = _load_venice_config().get(name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _cfg_number(name: str, default: float) -> float:
    value = _load_venice_config().get(name)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    env_model = os.getenv("VENICE_IMAGE_MODEL", "").strip()
    if env_model:
        return env_model, _MODELS.get(env_model, {"display": env_model})

    cfg = _load_venice_config()
    candidate = cfg.get("model") if isinstance(cfg.get("model"), str) else None
    if candidate:
        return candidate, _MODELS.get(candidate, {"display": candidate})

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _resolve_format() -> str:
    cfg = _load_venice_config()
    fmt = cfg.get("format") if isinstance(cfg.get("format"), str) else None
    if fmt and fmt.lower() in _FORMAT_EXTENSIONS:
        return fmt.lower()
    return DEFAULT_FORMAT


def _api_key() -> str:
    return (
        os.getenv("VENICE_API_KEY")
        or os.getenv("VENICE_INFERENCE_KEY")
        or os.getenv("VENICE_KEY")
        or ""
    ).strip()


def _decode_image_payload(image: str) -> str:
    """Return raw base64 from either a plain b64 string or data URI."""
    if image.startswith("data:image/") and "," in image:
        return image.split(",", 1)[1]
    return image


class VeniceImageGenProvider(ImageGenProvider):
    """Venice ``/api/v1/image/generate`` backend."""

    @property
    def name(self) -> str:
        return "venice"

    @property
    def display_name(self) -> str:
        return "Venice"

    def is_available(self) -> bool:
        return bool(_api_key())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta.get("display", model_id),
                "speed": meta.get("speed", ""),
                "strengths": meta.get("strengths", ""),
            }
            for model_id, meta in _MODELS.items()
        ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Venice",
            "badge": "paid",
            "tag": "Native Venice image generation via /api/v1/image/generate",
            "env_vars": [
                {
                    "key": "VENICE_API_KEY",
                    "prompt": "Venice API key",
                    "url": "https://venice.ai/settings/api",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        api_key = _api_key()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model_id, _meta = _resolve_model()
        fmt = _resolve_format()
        extension = _FORMAT_EXTENSIONS.get(fmt, "webp")

        if not api_key:
            return error_response(
                error="VENICE_API_KEY not set. Create a standard Venice API key and save it in this profile's .env.",
                error_type="missing_api_key",
                provider="venice",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        width, height = _DIMENSIONS.get(aspect, _DIMENSIONS["landscape"])
        payload: Dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "format": fmt,
            "return_binary": False,
            "variants": 1,
            "width": width,
            "height": height,
            "aspect_ratio": _ASPECT_RATIOS.get(aspect, "16:9"),
            "embed_exif_metadata": False,
            "safe_mode": _cfg_bool("safe_mode", DEFAULT_SAFE_MODE),
            "cfg_scale": _cfg_number("cfg_scale", DEFAULT_CFG_SCALE),
            "steps": int(_cfg_number("steps", DEFAULT_STEPS)),
        }

        negative_prompt = _load_venice_config().get("negative_prompt")
        if isinstance(negative_prompt, str) and negative_prompt.strip():
            payload["negative_prompt"] = negative_prompt.strip()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Venice supports Brotli, but this runtime may not have Brotli decoding
            # installed for requests/urllib3. Prefer gzip so response.json() works
            # consistently instead of receiving compressed bytes.
            "Accept-Encoding": "gzip",
        }
        base_url = os.getenv("VENICE_BASE_URL", "https://api.venice.ai/api/v1").strip().rstrip("/")

        try:
            response = requests.post(
                f"{base_url}/image/generate",
                headers=headers,
                json=payload,
                timeout=180,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response else 0
            try:
                body = exc.response.json()
                err_msg = body.get("error") or body.get("message") or str(body)[:400]
            except Exception:
                err_msg = exc.response.text[:400] if exc.response else str(exc)
            logger.error("Venice image gen failed (%d): %s", status, err_msg)
            return error_response(
                error=f"Venice image generation failed ({status}): {err_msg}",
                error_type="api_error",
                provider="venice",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.Timeout:
            return error_response(
                error="Venice image generation timed out (180s)",
                error_type="timeout",
                provider="venice",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.ConnectionError as exc:
            return error_response(
                error=f"Venice connection error: {exc}",
                error_type="connection_error",
                provider="venice",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            result = response.json()
        except Exception as exc:
            return error_response(
                error=f"Venice returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider="venice",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        images = result.get("images")
        if not isinstance(images, list) or not images:
            return error_response(
                error="Venice returned no images",
                error_type="empty_response",
                provider="venice",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = images[0]
        if not isinstance(first, str) or not first.strip():
            return error_response(
                error="Venice response image was empty or not a string",
                error_type="invalid_response",
                provider="venice",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            b64 = _decode_image_payload(first.strip())
            # Validate so save_b64_image fails loudly before writing partial junk.
            base64.b64decode(b64, validate=True)
            saved_path = save_b64_image(b64, prefix=f"venice_{model_id}", extension=extension)
        except Exception as exc:
            return error_response(
                error=f"Could not decode/save Venice image: {exc}",
                error_type="io_error",
                provider="venice",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {
            "format": fmt,
            "width": width,
            "height": height,
            "safe_mode": payload["safe_mode"],
            "venice_request_id": result.get("id"),
        }
        return success_response(
            image=str(saved_path),
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="venice",
            extra=extra,
        )


def register(ctx: Any) -> None:
    """Register this provider with the image-gen registry."""
    ctx.register_image_gen_provider(VeniceImageGenProvider())
