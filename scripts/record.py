"""Record a use case's clip for docs/use-cases.md: `media/<scene>.webp`.

    pip install playwright pillow && python -m playwright install chromium
    python scripts/record.py users-roles --base http://127.0.0.1:8000

Drives a running app (portal mode for the scenes that need a login —
FIREFLYER_USER / FIREFLYER_PASSWORD, admin/admin by default) in a headless
browser and writes an animated WebP in the shape every clip shares: 960×540,
dark theme, a dot for the cursor, a short fade in from black, looping. A frame
is a screenshot, held for as long as it really showed, so the clip plays at the
pace the scene ran.

A scene is a function of a `Clip`; add one to `SCENES`. It starts from the
data already in the app, so seed what it shows first (and keep it tidy:
whatever the scene creates, it removes again before it starts).
"""

import argparse
import io
import os
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

SIZE = {"width": 960, "height": 540}
MEDIA = Path(__file__).resolve().parent.parent / "media"
FADE_FRAMES = 8          # the fade in: this many frames from black …
FADE_MS = 60             # … each held this long
QUALITY = 80             # lossy WebP: the clips stay a few hundred KB

# The cursor, which a headless browser doesn't draw: the dot the clips show.
_CURSOR = """
addEventListener('DOMContentLoaded', () => {
  const dot = document.createElement('div');
  dot.id = 'ff-rec-cursor';
  dot.style.cssText = 'position:fixed;z-index:2147483647;width:12px;height:12px;margin:-6px 0 0 -6px;'
    + 'border-radius:50%;background:#d7f3fb;border:2px solid #20a7c9;pointer-events:none;'
    + 'left:' + (window.__ffCursor || [480, 300])[0] + 'px;top:' + (window.__ffCursor || [480, 300])[1] + 'px;'
    + 'transition:transform .12s';
  document.documentElement.appendChild(dot);
});
"""


class Clip:
    """The page, and the frames taken of it so far."""

    def __init__(self, page, base: str):
        self.page = page
        self.base = base.rstrip("/")
        self.frames: list[tuple[Image.Image, float]] = []   # (frame, when it was taken)
        self.at = (480, 300)

    # --- frames ---

    def snap(self) -> None:
        shot = self.page.screenshot(type="png")
        self.frames.append((Image.open(io.BytesIO(shot)).convert("RGB"), time.monotonic()))

    def hold(self, seconds: float) -> None:
        """Keep filming for `seconds` — what the viewer reads, or watches settle."""
        end = time.monotonic() + seconds
        self.snap()
        while time.monotonic() < end:
            self.snap()

    # --- the viewer's hand ---

    def goto(self, path: str) -> None:
        self.page.goto(self.base + path)
        self.page.wait_for_load_state("networkidle")
        self._dot()

    def move(self, selector: str, steps: int = 12) -> None:
        box = self.page.locator(selector).first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        x0, y0 = self.at
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)                      # ease in and out
            self.at = (x0 + (x - x0) * t, y0 + (y - y0) * t)
            self.page.mouse.move(*self.at)
            self._dot()
            self.snap()

    def click(self, selector: str, settle: float = 0.4) -> None:
        self.move(selector)
        self._dot(press=True)
        self.page.locator(selector).first.click()
        self._dot()
        self.hold(settle)

    def type(self, text: str, per_key: float = 0.07) -> None:
        for ch in text:
            self.page.keyboard.type(ch)
            self.hold(per_key)

    def press(self, key: str, settle: float = 0.3) -> None:
        self.page.keyboard.press(key)
        self.hold(settle)

    def _dot(self, press: bool = False) -> None:
        self.page.evaluate(
            """([x, y, press]) => {
                 window.__ffCursor = [x, y];
                 const dot = document.getElementById('ff-rec-cursor');
                 if (!dot) return;
                 // A modal dialog is in the top layer, over everything else:
                 // the dot rides inside it while one is open.
                 const host = document.querySelector('dialog[open]') || document.documentElement;
                 if (dot.parentNode !== host) host.appendChild(dot);
                 dot.style.left = x + 'px'; dot.style.top = y + 'px';
                 dot.style.transform = press ? 'scale(.7)' : '';
               }""",
            [*self.at, press],
        )

    # --- the file ---

    def save(self, path: Path) -> int:
        frames = [f for f, _ in self.frames]
        durations = [max(20, int((b - a) * 1000)) for (_, a), (_, b) in zip(self.frames, self.frames[1:])]
        durations.append(1500)                           # the last frame lingers before the loop
        black = Image.new("RGB", frames[0].size)
        fade = [Image.blend(black, frames[0], i / FADE_FRAMES) for i in range(FADE_FRAMES)]
        frames, durations = fade + frames, [FADE_MS] * FADE_FRAMES + durations
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations,
                       loop=0, quality=QUALITY, method=6)
        return path.stat().st_size


def _login(clip: Clip) -> None:
    clip.page.goto(clip.base + "/login")
    if "/login" not in clip.page.url:
        return                                           # no login: not portal mode
    clip.page.fill("input[name=username]", os.environ.get("FIREFLYER_USER", "admin"))
    clip.page.fill("input[name=password]", os.environ.get("FIREFLYER_PASSWORD", "admin"))
    clip.page.press("input[name=password]", "Enter")
    clip.page.wait_for_load_state("networkidle")


# --- scenes ----------------------------------------------------------------------


def users_roles(clip: Clip) -> None:
    """docs/use-cases.md → "Add users and their roles" (portal mode). Needs a
    `viewer` role with filtered access to a dataset a dashboard reads, so
    viewing as the new user shows the narrowing; makes the user `carol`."""
    page = clip.page
    page.goto(clip.base + "/users")
    old = page.locator("button[title=Edit][data-name=carol]")
    if old.count():
        page.request.post(f"{clip.base}/users/{old.first.get_attribute('data-id')}/delete")

    clip.goto("/users")
    clip.hold(1.2)
    clip.click("th.actions button")
    clip.type("carol")
    clip.click("#user-form input[name=first_name]", settle=0.1)
    clip.type("Carol")
    clip.click("#user-form input[name=last_name]", settle=0.1)
    clip.type("Diaz")
    clip.click("#user-form input[name=email]", settle=0.1)
    clip.type("carol@example.com", per_key=0.04)
    clip.click("#user-form input[name=password]", settle=0.1)
    clip.type("carol-pass-1", per_key=0.05)
    clip.click(".ff-multi-box", settle=0.8)
    clip.type("vie", per_key=0.12)
    clip.press("Enter", settle=0.5)
    for _ in "vie":                                       # the whole list again: admin struck out
        clip.press("Backspace", settle=0.08)
    clip.move(".ff-multi-item:has(input[data-name=admin])")
    clip.hold(1.4)
    clip.press("Escape")
    clip.click("#user-form button.ok", settle=1.4)

    # …and the dashboard as carol sees it, through her role.
    clip.click(".ff-switch a[href='/']", settle=0.6)
    clip.click(".dash-table a.name", settle=1.2)
    clip.click("#ff-role summary", settle=0.4)
    clip.click(".ff-role-kinds button[data-kind=user]", settle=0.3)
    clip.type("car", per_key=0.12)
    clip.press("Enter", settle=2.4)


SCENES = {"users-roles": users_roles}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scene", choices=sorted(SCENES))
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=SIZE, color_scheme="dark")
        page.add_init_script(_CURSOR)
        clip = Clip(page, args.base)
        _login(clip)
        SCENES[args.scene](clip)
        out = MEDIA / f"{args.scene}.webp"
        size = clip.save(out)
        browser.close()
    print(f"{out} — {len(clip.frames)} frames, {size // 1024} KB")


if __name__ == "__main__":
    main()
