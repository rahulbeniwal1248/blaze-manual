from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_ids: set[int]
    mongo_uri: str | None
    mongo_db: str
    data_file: Path
    session_dir: Path
    status_update_every: int


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    load_dotenv()
    owner_ids = {
        int(item.strip())
        for item in os.getenv("OWNER_IDS", "").split(",")
        if item.strip()
    }
    if not owner_ids:
        raise RuntimeError("OWNER_IDS must contain at least one Telegram user ID")

    session_dir = Path(os.getenv("SESSION_DIR", "sessions"))
    session_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        api_id=int(_required("API_ID")),
        api_hash=_required("API_HASH"),
        bot_token=_required("BOT_TOKEN"),
        owner_ids=owner_ids,
        mongo_uri=os.getenv("MONGO_URI") or None,
        mongo_db=os.getenv("MONGO_DB", "blaze_forward_bot"),
        data_file=Path(os.getenv("DATA_FILE", "data.json")),
        session_dir=session_dir,
        status_update_every=max(1, int(os.getenv("STATUS_UPDATE_EVERY", "10"))),
    )
