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


def url(name: str) -> str:
    """A static file's versioned URL — for an import-map entry, which a module
    imports by name, so the version can't ride on a <script> tag."""
    return _url(name)


def stylesheets(*names: str) -> str:
    """`<link>` tags for `names`, in order — later files win the cascade."""
    return "\n".join(f'<link rel="stylesheet" href="{_url(n)}">' for n in names)


def script(name: str) -> str:
    return f'<script src="{_url(name)}"></script>'


def module(name: str) -> str:
    """An ES module — deferred by nature, so it runs once the page is parsed,
    after the classic scripts before it."""
    return f'<script type="module" src="{_url(name)}"></script>'


def favicons() -> str:
    """The tab icon every page links: `favicon.svg`, the logo redrawn to read
    at 16px (see its comment). SVG only — every current browser takes one."""
    return f'<link rel="icon" type="image/svg+xml" href="{_url("favicon.svg")}">'
