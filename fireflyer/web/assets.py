"""The editor's own stylesheets and scripts, served as files from `static/`.

Editor chrome only — chart and dashboard output still inline their CSS, since a
`to_html()` page has to stand alone with nothing to fetch.

The links carry the file's modification time as `?v=`, so an edited file gets a
new URL and the browser fetches it on the next page load instead of reusing a
cached copy. `app.py` mounts the folder at `/static`, read from disk per request,
so a CSS or JS edit needs no server restart (uvicorn's `--reload` only watches
`.py` files).
"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"


def _url(name: str) -> str:
    version = int((STATIC_DIR / name).stat().st_mtime)
    return f"/static/{name}?v={version}"


def stylesheets(*names: str) -> str:
    """`<link>` tags for `names`, in order — later files win the cascade."""
    return "\n".join(f'<link rel="stylesheet" href="{_url(n)}">' for n in names)


def script(name: str) -> str:
    return f'<script src="{_url(name)}"></script>'
