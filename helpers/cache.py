__all__ = ("CACHE",)

from typing import Any


class _Singleton:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        return self._store[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._store[key] = value

    def __getattr__(self, key: str) -> Any:
        if key in self._store:
            return self._store[key]
        raise AttributeError(f"The CACHE has no cached value '{key}'")

    def __setattr__(self, key: str, value: Any) -> None:
        if key == "_store":
            super().__setattr__(key, value)
        else:
            self._store[key] = value


CACHE = _Singleton()
