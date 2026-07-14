"""
Logging configuration.

httpx logs the full request URL at INFO level. Even with the token moved to the
body, that is noise; and one careless change putting a secret back in the query
string would silently start leaking it again. Rather than trust that nobody
makes that change, the logger is muted at the source.
"""

import logging


def configure(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # Muted deliberately — see module docstring.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
