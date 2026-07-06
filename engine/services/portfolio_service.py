import json
import threading
import uuid
from pathlib import Path

from engine.settings import settings

_lock = threading.Lock()


def _load() -> dict:
    path = Path(settings.portfolio_data_path)
    if not path.exists():
        return {"positions": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    # one-time migration: rename old key
    if any("purchase_price" in p and "purchase_price_dirty" not in p for p in data.get("positions", [])):
        for p in data["positions"]:
            if "purchase_price" in p and "purchase_price_dirty" not in p:
                p["purchase_price_dirty"] = p.pop("purchase_price")
        _save(data)
    return data


def _save(data: dict) -> None:
    Path(settings.portfolio_data_path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Positions ─────────────────────────────────────────────────────────────────

def get_positions() -> list[dict]:
    with _lock:
        return _load().get("positions", [])


def get_position_by_id(position_id: str) -> dict | None:
    with _lock:
        return next(
            (p for p in _load().get("positions", []) if p["id"] == position_id),
            None,
        )


def add_position(position: dict) -> dict:
    with _lock:
        data = _load()
        record = {**position, "id": str(uuid.uuid4())}
        data["positions"].append(record)
        _save(data)
        return record


def update_position(position_id: str, updates: dict) -> dict | None:
    with _lock:
        data = _load()
        for i, p in enumerate(data["positions"]):
            if p["id"] == position_id:
                data["positions"][i] = {**p, **updates, "id": position_id}
                _save(data)
                return data["positions"][i]
        return None


def delete_position(position_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["positions"])
        data["positions"] = [p for p in data["positions"] if p["id"] != position_id]
        if len(data["positions"]) < before:
            _save(data)
            return True
        return False
