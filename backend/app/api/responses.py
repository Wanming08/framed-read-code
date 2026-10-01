"""The JSON result envelope used by the upstream Spring controllers."""

from typing import Any


def ok(data: Any = None) -> dict[str, Any]:
    return {"code": 0, "message": "success", "data": data}


def error(code: int, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "data": None}
