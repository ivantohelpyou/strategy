"""The admin surface over the strategy store.

Read-only, and deliberately unstyled beyond legibility: this is where a
capability shows up FIRST, in the rawest form that is still honest. Features
graduate from here into curated, purpose-built surfaces once they have earned a
shape. Nothing here is a product screen.

Position #67 puts the store's human surface outside the CLI; this app ships
with the CLI so the tool is showable without a deploy (position #48). No
private rows live in this package — it renders whatever store STRATEGY_DB_URL
points at.
"""

from .server import create_app

__all__ = ["create_app"]
