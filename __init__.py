"""Leonardo.AI video generation backend for Hermes.

Why this plugin exists
----------------------
Hermes ships video backends for FAL, xAI and DeepInfra. Leonardo is a large
hosted video provider that Hermes cannot reach at all — and it is the *cheapest
practical* door to a hosted video model, because one Leonardo account fronts
several families (its own Motion models plus Veo, Kling, Seedance and Wan)
instead of requiring an account with every vendor.

This plugin adds Leonardo as a first-class ``video_gen`` provider:

  * text-to-video  — ``video_generate(prompt=...)``
  * image-to-video — ``video_generate(prompt=..., image_url=...)``

Routing is automatic and invisible to the agent, matching the contract every
other Hermes video provider follows.

API contract (verified against the live API, not inferred)
----------------------------------------------------------
  POST https://cloud.leonardo.ai/api/rest/v2/generations   -> generation id
  GET  https://cloud.leonardo.ai/api/rest/v1/generations/{} -> generations_by_pk
       .status                      PENDING | COMPLETE | FAILED
       .generated_images[0].motionMP4URL   <- the video

Auth: ``LEONARDO_API_KEY``. Note Leonardo bills the REST API from **API
credits**, which are separate from web-app subscription tokens — an account can
show web tokens while having zero API credits. ``is_available()`` therefore
checks the key, and a 402/403 from the API surfaces as a clear error rather
than a silent failure.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from agent.video_gen_provider import (
    VideoGenProvider,
    error_response,
    success_response,
)

logger = logging.getLogger(__name__)

API_BASE = "https://cloud.leonardo.ai/api/rest"
CREATE_URL = f"{API_BASE}/v2/generations"
FETCH_URL = f"{API_BASE}/v1/generations/{{}}"

# Motion 2.0 is the documented, stable Leonardo video model. The dimension
# table below is copied from Leonardo's own documentation for this model.
# Further families (Veo / Kling / Seedance / Wan) are fronted by the same
# account and can be added here once their model ids are verified live —
# the rest of this plugin is family-agnostic.
FAMILIES: Dict[str, Dict[str, Any]] = {
    "motion-2.0": {
        "model": "motion_2.0",
        "display": "Leonardo Motion 2.0",
        "strengths": "Leonardo's own video model. Solid motion, cheap, fast.",
        "speed": "~1-3 min",
        "price": "Leonardo API credits",
        "modalities": ["text", "image"],
        # (aspect_ratio, resolution) -> (width, height)
        "dimensions": {
            ("16:9", "720p"): (1280, 720),
            ("16:9", "480p"): (832, 480),
            ("9:16", "720p"): (720, 1280),
            ("9:16", "480p"): (480, 832),
            ("4:5", "480p"): (576, 720),
            ("2:3", "480p"): (512, 768),
        },
    },
}

DEFAULT_FAMILY = "motion-2.0"
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 600.0


class LeonardoVideoGenProvider(VideoGenProvider):
    """Leonardo.AI video generation (Motion 2.0)."""

    # ------------------------------------------------------------------ meta

    @property
    def name(self) -> str:
        return "leonardo"

    @property
    def display_name(self) -> str:
        return "Leonardo AI"

    def is_available(self) -> bool:
        return bool(self._api_key())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": fid,
                "display": fam.get("display", fid),
                "speed": fam.get("speed", ""),
                "strengths": fam.get("strengths", ""),
                "price": fam.get("price", ""),
                "modalities": fam.get("modalities", ["text"]),
            }
            for fid, fam in FAMILIES.items()
        ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "Video",
            "tag": "Text + image to video via Leonardo API credits",
            "env_vars": [
                {
                    "key": "LEONARDO_API_KEY",
                    "label": "Leonardo API key",
                    "help": "app.leonardo.ai -> API Access -> Create New Key. "
                            "API credits are purchased separately from web tokens.",
                    "required": True,
                }
            ],
        }

    def default_model(self) -> Optional[str]:
        return DEFAULT_FAMILY

    def capabilities(self) -> Dict[str, Any]:
        dims = FAMILIES[DEFAULT_FAMILY]["dimensions"]
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": sorted({a for a, _ in dims}),
            "resolutions": sorted({r for _, r in dims}),
            # Leonardo's Motion API does not accept a duration parameter; the
            # clip length is fixed by the model. Report the truthful single
            # value rather than pretending we can vary it.
            "max_duration": 5,
            "min_duration": 5,
            "supports_audio": False,
            "supports_negative_prompt": True,
            "max_reference_images": 1,   # start_frame guidance
        }

    # ------------------------------------------------------------ api helpers

    def _api_key(self) -> str:
        return (os.environ.get("LEONARDO_API_KEY") or "").strip()

    def _request(self, method: str, url: str, payload: Optional[dict] = None) -> dict:
        key = self._api_key()
        if not key:
            raise RuntimeError("LEONARDO_API_KEY is not set")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("accept", "application/json")
        req.add_header("authorization", f"Bearer {key}")
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code in (401, 403):
                raise RuntimeError(
                    f"Leonardo rejected the key ({e.code}). Check LEONARDO_API_KEY."
                ) from e
            if e.code == 402:
                raise RuntimeError(
                    "Leonardo has no API credits (402). API credits are separate "
                    "from web-app subscription tokens — top up under API Access."
                ) from e
            raise RuntimeError(f"Leonardo HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Leonardo unreachable: {e.reason}") from e
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Leonardo returned non-JSON: {body[:200]}") from e

    @staticmethod
    def _extract_id(payload: dict) -> str:
        """Leonardo has shipped several id shapes — accept all of them.

        Observed live against the v2 endpoint::

            {"generate": {"generationId": "...", "cost": {"amount": "0.299",
                                                          "unit": "DOLLARS"}}}

        Older/v1 shapes nest under ``sdGenerationJob``. Missing any one of
        these means a *billed* generation is thrown away, so the list is
        deliberately permissive.
        """
        for path in (
            ("generate", "generationId"),          # v2 (verified live)
            ("generationId",),
            ("sdGenerationJob", "generationId"),   # v1
            ("data", "generationId"),
            ("id",),
        ):
            cur: Any = payload
            for k in path:
                if not isinstance(cur, dict) or k not in cur:
                    cur = None
                    break
                cur = cur[k]
            if isinstance(cur, str) and cur:
                return cur
        raise RuntimeError(f"No generation id in Leonardo response: {json.dumps(payload)[:300]}")

    @staticmethod
    def _extract_cost(payload: dict) -> Dict[str, Any]:
        """Pull the reported cost out of the create response, when present."""
        gen = payload.get("generate") if isinstance(payload, dict) else None
        cost = (gen or {}).get("cost") if isinstance(gen, dict) else None
        return cost if isinstance(cost, dict) else {}

    def _await_video(self, generation_id: str) -> Dict[str, Any]:
        """Poll until the generation finishes; return the generation record."""
        deadline = time.time() + POLL_TIMEOUT
        delay = POLL_INTERVAL
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            payload = self._request("GET", FETCH_URL.format(generation_id))
            gen = payload.get("generations_by_pk") or {}
            last = gen
            status = str(gen.get("status") or "").upper()
            if status == "COMPLETE":
                return gen
            if status == "FAILED":
                raise RuntimeError("Leonardo reported the generation FAILED")
            time.sleep(delay)
            delay = min(delay * 1.4, 20.0)
        raise RuntimeError(
            f"Leonardo generation {generation_id} did not finish within "
            f"{int(POLL_TIMEOUT)}s (last status: {last.get('status')!r})"
        )

    # -------------------------------------------------------------- generation

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,                      # forward-compat: MUST be ignored
    ) -> Dict[str, Any]:
        family_id = model if model in FAMILIES else DEFAULT_FAMILY
        fam = FAMILIES[family_id]
        modality = "image" if image_url else "text"

        params: Dict[str, Any] = {
            "prompt": prompt,
            "quantity": 1,
            "prompt_enhance": "OFF",        # deterministic: never rewrite user words
            "frame_interpolation": True,
        }
        if negative_prompt:
            params["negative_prompt"] = negative_prompt[:1000]
        if seed is not None:
            params["seed"] = int(seed) % 2147483638

        # dimensions: both or neither
        dims = fam["dimensions"]
        wh = dims.get((aspect_ratio, resolution))
        if wh is None:                       # fall back to the closest known ratio
            for (ar, res), val in dims.items():
                if res == resolution:
                    wh = val
                    aspect_ratio = ar
                    break
        if wh:
            params["width"], params["height"] = wh

        # image-to-video: seed the first frame
        if image_url:
            params["guidances"] = {
                "start_frame": [{"image": {"id": image_url, "type": "URL"}}]
            }

        body = {"model": fam["model"], "public": False, "parameters": params}

        cost: Dict[str, Any] = {}
        try:
            created = self._request("POST", CREATE_URL, body)
            gen_id = self._extract_id(created)
            cost = self._extract_cost(created)
            logger.info("leonardo: submitted %s (%s, %s)", gen_id, family_id, modality)
            gen = self._await_video(gen_id)
        except Exception as e:                # noqa: BLE001 — surface, never crash
            return error_response(
                error=str(e), provider="leonardo", model=family_id, prompt=prompt
            )

        images = gen.get("generated_images") or []
        video = ""
        for img in images:
            video = img.get("motionMP4URL") or ""
            if video:
                break
        if not video:
            return error_response(
                error="Leonardo completed but returned no motionMP4URL "
                      "(did the request omit a video model?)",
                provider="leonardo", model=family_id, prompt=prompt,
            )

        return success_response(
            video=video,
            model=family_id,
            prompt=prompt,
            modality=modality,
            aspect_ratio=aspect_ratio if wh else "",
            duration=0,                       # Motion API fixes clip length
            provider="leonardo",
            extra={
                "generation_id": gen.get("id", ""),
                "motion_model": gen.get("motionModel") or "",
                "endpoint": CREATE_URL,
                "cost": cost,
            },
        )


def register(ctx) -> None:
    """Plugin entry point — wire the Leonardo provider into the registry."""
    ctx.register_video_gen_provider(LeonardoVideoGenProvider())
