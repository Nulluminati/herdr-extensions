"""The Git environment that the tests which make commits run with.

Git reads these variables before the machine's config, so a commit needs no
global user.name, user.email or commit.gpgsign.
"""

import os

IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}


def isolated():
    """The environment for a commit that does not depend on the machine's Git config."""
    return {**IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
