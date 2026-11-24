"""Trading strategies, auto-registered by name.

Add a strategy by dropping a module into this package that defines a subclass
of :class:`~papertrader.strategies.base.Strategy` decorated with
``@STRATEGIES.register("my_name")``.
"""

from papertrader.registry import autoload
from papertrader.strategies.base import STRATEGIES, Strategy

autoload(__name__)

__all__ = ["STRATEGIES", "Strategy"]
