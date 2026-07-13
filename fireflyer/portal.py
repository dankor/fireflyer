"""`python -m fireflyer.portal` — launch the editor in portal mode.

Portal mode stores dashboards in a database and lists them in a gallery (see
`fireflyer.web.portal` for the store itself). This entrypoint reads runtime
config from `portal.yaml` (title, database url), lets the environment override
it (`DATABASE_URL`, `FIREFLYER_PORTAL_TITLE`), flips `FIREFLYER_PORTAL` on, and
serves on 0.0.0.0 for containers. The plain `python -m fireflyer.web`
entrypoint stays on 127.0.0.1 in single-dashboard mode.
"""

import os
from pathlib import Path

import uvicorn
import yaml


def _load_config() -> dict:
    path = Path(os.environ.get("FIREFLYER_PORTAL_CONFIG", "portal.yaml"))
    if path.exists():
        return yaml.safe_load(path.read_text()) or {}
    return {}


def main() -> None:
    cfg = _load_config()
    os.environ["FIREFLYER_PORTAL"] = "1"
    # Env wins over the file, so a container can override without editing yaml.
    if cfg.get("title") and "FIREFLYER_PORTAL_TITLE" not in os.environ:
        os.environ["FIREFLYER_PORTAL_TITLE"] = cfg["title"]
    if cfg.get("database_url") and "DATABASE_URL" not in os.environ:
        os.environ["DATABASE_URL"] = cfg["database_url"]

    uvicorn.run(
        "fireflyer.web.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_includes=["*.py", "*.css", "*.html"],
    )


if __name__ == "__main__":
    main()
