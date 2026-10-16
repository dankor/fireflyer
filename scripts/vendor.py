"""Download the editor's third-party browser code into fireflyer/web/static/vendor,
so the editor runs with no network.

    python scripts/vendor.py

Not a build step: it fetches the exact files a CDN would serve, once, and they
are committed. Run it again only to change a version below. It writes the
import map's entries too (printed at the end, for editor.html).

- htmx, as its own minified file.
- CodeMirror 6 and its dependencies, as the ES modules esm.sh builds. The `*`
  form keeps every import a bare package name, so each file stands alone and
  the import map in editor.html resolves them all to these files — one copy of
  each, which CodeMirror needs (two copies of @codemirror/state break it).

Every file is checked before anything is written: one that would still reach
for the network (an absolute URL or a root-relative esm.sh path) fails the run.
"""

import re
import sys
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "fireflyer" / "web" / "static" / "vendor"

HTMX = ("htmx.org", "1.9.12", "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js")

# Every package the editor's modules import, at one version each.
MODULES = {
    "@codemirror/state": "6.7.6",
    "@codemirror/view": "6.43.14",
    "@codemirror/commands": "6.11.1",
    "@codemirror/language": "6.13.1",
    "@codemirror/search": "6.7.2",
    "@codemirror/autocomplete": "6.20.3",
    "@codemirror/lint": "6.9.7",
    "@codemirror/lang-yaml": "6.1.3",
    "@codemirror/merge": "6.12.2",
    "@codemirror/streamparser": "6.0.0",
    "@lezer/common": "1.5.3",
    "@lezer/lr": "1.4.11",
    "@lezer/yaml": "1.0.4",
    "@lezer/highlight": "1.2.5",
    "style-mod": "4.1.4",
    "w3c-keyname": "2.2.8",
    "crelt": "1.0.7",
    "@marijn/find-cluster-break": "1.0.4",
}


# esm.sh polyfills Node globals a package reads. The only one here: @lezer/lr
# reads `process.env.LOG` to switch on its debug logging, and esm.sh imports a
# whole Node `process` from its own server for that. An empty stand-in is all
# a browser needs. Each patch must match exactly once, or the run fails — a
# changed file is looked at, not patched blindly.
PATCHES = {
    "@lezer/lr": [('import __Process$ from "/node/process.mjs";', "const __Process$ = {env: {}};")],
}


def local_name(package: str, version: str) -> str:
    """`@codemirror/state` 6.7.6 -> `codemirror-state@6.7.6.mjs`."""
    return package.lstrip("@").replace("/", "-") + f"@{version}.mjs"


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def module_source(package: str, version: str) -> str:
    """esm.sh answers `*pkg@ver` with a one-line stub re-exporting the built
    file; this fetches the built file itself."""
    stub = fetch(f"https://esm.sh/*{package}@{version}")
    path = re.search(r'from\s*"(/\*[^"]+\.mjs)"', stub)
    if not path:
        sys.exit(f"{package}: unexpected esm.sh stub:\n{stub[:300]}")
    return fetch("https://esm.sh" + path.group(1))


def check(name: str, source: str) -> set[str]:
    """The bare packages `source` imports; fail if it imports anything else."""
    specifiers = set(re.findall(r'(?:from\s*|import\s*\(?\s*)"([^"]+)"', source))
    remote = sorted(s for s in specifiers if s.startswith(("/", "http:", "https:", ".")))
    if remote:
        sys.exit(f"{name} would still load from the network: {remote}")
    return specifiers


def main() -> None:
    files = {f"htmx@{HTMX[1]}.min.js": fetch(HTMX[2])}
    imports = {}
    for package, version in MODULES.items():
        name = local_name(package, version)
        source = module_source(package, version)
        for old, new in PATCHES.get(package, []):
            if source.count(old) != 1:
                sys.exit(f"{package}: patch no longer matches exactly once: {old}")
            source = source.replace(old, new)
        files[name] = source
        imports[package] = f"/static/vendor/{name}"
    for name, source in files.items():
        if name.endswith(".mjs"):
            missing = check(name, source) - set(MODULES)
            if missing:
                sys.exit(f"{name} imports packages not vendored: {sorted(missing)}")
    VENDOR.mkdir(parents=True, exist_ok=True)
    for old in VENDOR.glob("*"):
        if old.name not in files and old.name != "README.md":
            old.unlink()
    for name, source in files.items():
        (VENDOR / name).write_text(source)
    print(f"{len(files)} files in {VENDOR}")
    print("\nimport map for editor.html:")
    for package, url in imports.items():
        print(f'  "{package}": "{url}",')


if __name__ == "__main__":
    main()
