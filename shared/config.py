"""Environment loading and the AgentVerse 2026 model registry.

Import model IDs from here; never hardcode model strings elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load .env from the repo root if present. Real environment variables win.
load_dotenv(REPO_ROOT / ".env", override=False)

MODEL_IDS: dict[str, str] = {
    "lead_reasoning": "gemini-4-argon",
    "managed_sandbox": "antigravity-preview-09-2026",
    "deep_research": "deep-research-preview-04-2026",
    "fast_subagent": "gemini-3.8-flash",
    "fast_router": "gemini-3.5-flash-lite",
    "live_voice_vision": "gemini-3.8-live",
    "live_extended_thinking": "gemini-3.8-live-extended-thinking",
    "transcribe_live": "gemini-3.5-transcribe-live",
    "transcribe_batch": "gemini-3.5-transcribe",
    "live_translate": "gemini-3.5-live-translate-preview",
    "tts_studio": "gemini-3.8-flash-tts",
    "tts_fast": "gemini-3.8-flash-lite-tts",
    "image_4k": "gemini-nano-banana-2.1",
    "image_fast": "gemini-3.1-flash-lite-image",
    "video_audio": "gemini-omni-1.1-flash",
    "video_cinematic_vertex": "veo-3.1-generate-001",
    "music_full_song": "lyria-3.5",
    "music_loop": "lyria-3-clip-preview",
    "open_model": "gemma-4-31b-it",
}

# Allowed values for generation_config["thinking_level"] (Interactions API).
THINKING_LEVELS = ("low", "medium", "high")

CHECK_IDS = ("science", "trial", "market")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
CACHE_DIR: Path = Path(os.getenv("PREMORTEM_CACHE_DIR", str(REPO_ROOT / ".cache")))
CHECK_TIMEOUT_S: float = _env_float("PREMORTEM_CHECK_TIMEOUT_S", 45.0)
# Analyst rejections, kept across runs so the system remembers where it was wrong.
REJECTIONS_PATH: Path = Path(os.getenv("PREMORTEM_REJECTIONS_PATH", str(REPO_ROOT / "data" / "rejections.jsonl")))


def model_id(role: str) -> str:
    """Return the model ID for a role key, failing loudly on typos."""
    try:
        return MODEL_IDS[role]
    except KeyError as exc:
        raise KeyError(f"Unknown model role {role!r}. Known: {sorted(MODEL_IDS)}") from exc


def require_gemini_key() -> str:
    """Return the Gemini API key or raise with a setup hint."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
    return GEMINI_API_KEY
