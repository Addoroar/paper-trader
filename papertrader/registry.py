"""A tiny plugin registry used for strategies and selectors.

Classes register themselves with a decorator; importing the package that
contains them (``papertrader.strategies`` / ``papertrader.selection``)
auto-imports every module in it, so dropping a new file into the folder is
enough to make a plugin available by name in ``config.yaml``.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Callable


class Registry[T]:
    """Name -> class mapping with a ``register`` decorator."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, type[T]] = {}

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        """Class decorator registering ``cls`` under ``name``."""

        def deco(cls: type[T]) -> type[T]:
            if name in self._items and self._items[name] is not cls:
                raise ValueError(f"duplicate {self.kind} name: {name!r}")
            self._items[name] = cls
            cls.name = name  # type: ignore[attr-defined]
            return cls

        return deco

    def get(self, name: str) -> type[T]:
        """Return the class registered as ``name`` or raise ``KeyError`` with the options."""
        try:
            return self._items[name]
        except KeyError:
            raise KeyError(
                f"unknown {self.kind} {name!r}; available: {', '.join(sorted(self._items))}"
            ) from None

    def names(self) -> list[str]:
        """Sorted list of registered names."""
        return sorted(self._items)

    def __contains__(self, name: object) -> bool:
        return name in self._items


def autoload(package_name: str) -> None:
    """Import every module in ``package_name`` so their decorators run."""
    pkg = importlib.import_module(package_name)
    for mod in pkgutil.iter_modules(pkg.__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{package_name}.{mod.name}")
