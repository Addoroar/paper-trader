"""Symbol selectors, auto-registered by name.

Add a selector by dropping a module into this package that subclasses
:class:`~papertrader.selection.base.Selector`, implements ``score`` and is
decorated with ``@SELECTORS.register("my_name")``.
"""

from papertrader.registry import autoload
from papertrader.selection.base import SELECTORS, ScoredSymbol, SelectionResult, Selector

autoload(__name__)

__all__ = ["SELECTORS", "ScoredSymbol", "SelectionResult", "Selector"]
