"""Release identity and staleness detection.

A silently stale release once pinned launchd to old code for several runs while
the working tree looked correct. Code that runs must be able to say which
version it is and whether the working tree has moved on.
"""

import hashlib
import os
from typing import Dict, Optional


def fingerprint(root: str) -> Optional[str]:
    """Stable hash of the operator source under root."""
    paths = [os.path.join(root, "run.py")]
    farm_dir = os.path.join(root, "farm")
    try:
        paths.extend(
            sorted(
                os.path.join(farm_dir, name)
                for name in os.listdir(farm_dir)
                if name.endswith(".py")
            )
        )
    except OSError:
        return None
    digest = hashlib.sha256()
    for path in paths:
        try:
            with open(path, "rb") as fh:
                digest.update(fh.read())
        except OSError:
            return None
    return digest.hexdigest()[:12]


def status() -> Dict[str, Optional[str]]:
    """Compare the running tree with the project working tree.

    The running tree reaches the project through the state symlink, which always
    points back at the project directory regardless of which release is live.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    running_root = os.path.dirname(here)
    project_root = os.path.realpath(os.path.join(running_root, "state", ".."))

    rev = None
    try:
        with open(os.path.join(running_root, "RELEASED")) as fh:
            rev = fh.read().strip()
    except OSError:
        rev = "working-tree"

    running = fingerprint(running_root)
    project = fingerprint(project_root)
    return {
        "rev": rev,
        "running": running,
        "project": project,
        "stale": "yes" if (running and project and running != project) else "no",
    }


def line() -> str:
    info = status()
    suffix = ""
    if info["stale"] == "yes":
        suffix = " | WORKING TREE DIFFERS - run deploy/release.sh to publish it"
    return "release: %s (%s)%s" % (info["rev"], info["running"], suffix)
