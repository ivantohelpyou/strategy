"""Import target for `--reload`, which needs an app it can re-import by name.

No `init_db()` here either: see `strategy web`. The store must already exist.
"""

from .server import create_app

app = create_app()
