from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_DATA: dict[str, Any] = {
    "channels": {},
    "forwarders": {},
    "settings": {
        "caption": "{caption}",
        "button_text": "",
        "button_url": "",
        "min_mb": 0,
        "max_mb": 0,
        "keywords": [],
        "blocked_extensions": [],
        "allow_text": True,
        "allow_photo": True,
        "allow_video": True,
        "allow_document": True,
        "allow_audio": True,
        "allow_voice": True,
        "allow_animation": True,
        "allow_sticker": True,
        "allow_poll": True,
        "skip_duplicates": True,
        "regex": "",
        "regex_mode": "include",
        "replacements": [],
    },
    "duplicates": [],
    "live_jobs": {},
}


class JsonStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = DEFAULT_DATA.copy()
        self.load()

    def load(self) -> None:
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = self._merge(DEFAULT_DATA, loaded)
        else:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def _merge(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        merged = json.loads(json.dumps(base))
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._merge(merged[key], value)
            else:
                merged[key] = value
        return merged
