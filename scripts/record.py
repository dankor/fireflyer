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

    def tap(self, selector: str, settle: float = 0.4) -> None:
        """A click where the cursor lands, as a hand makes it — no scrolling
        first, unlike a locator's click: a panel kept on screen (the filter
        badge's) stays where it is, and a control lit by hovering is hit."""
        self.move(selector)
        self._dot(press=True)
        self.page.mouse.click(*self.at)
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


def groups(clip: Clip) -> None:
    """docs/use-cases.md → "Share across groups" (portal mode), signed in as
    bob (FIREFLYER_USER=bob). Needs alice's dashboard "Alice report" on her
    `orders`, shared with a role bob holds through filters; makes bob's "My
    copy"."""
    page = clip.page
    page.request.post(f"{clip.base}/d/_bob/My%20copy/delete")

    clip.goto("/d/_bob")
    clip.hold(1.2)
    clip.click("#ff-group summary", settle=0.6)
    clip.click("#ff-group .ff-group-kinds button[data-kind=personal]", settle=0.5)  # opens on shared
    clip.click("#ff-group a[href='/d/_alice']", settle=1.0)
    clip.click(".dash-table a.name:has-text('Alice report')", settle=2.6)   # view-only, his rows
    clip.click(".ff-back", settle=0.8)
    clip.click("button[title=Clone][data-name='Alice report']", settle=0.4)
    page.fill("#clone-name", "")
    clip.type("My copy")
    clip.click("#clone-form button.ok", settle=2.0)                         # his own, to edit
    clip.click("#ff-modes button[data-mode=code]", settle=2.4)               # reading `_alice:orders`


def edit_chart(clip: Clip) -> None:
    """docs/use-cases.md → "Edit a chart with UI" (local paths mode, the `demo`
    path's "Orders overview", its Orders tab): the "big orders" table's form
    beside its preview, which follows every change — a new title and
    description, a column dragged to the front and one removed — and is live:
    its filter button shows the description and the chart's filter. Cancels
    at the end, so the file in git is untouched."""
    page = clip.page
    clip.goto("/d/demo/orders-overview")
    clip.hold(0.8)
    clip.click("#ff-modes button[data-mode=edit]", settle=0.5)
    clip.click(".fireflyer-tab[data-tab-name=Orders]", settle=1.4)
    # The pencil shows on hover: a click where the cursor is, as a hand makes
    # it (a locator's click checks the spot first, before the hover lit it).
    cell = ".fireflyer-dashboard-cell[data-cid=big_orders]"
    clip.move(cell)
    clip.hold(0.3)
    clip.move(f"{cell} .fireflyer-edit-btn")
    page.mouse.click(*clip.at)
    page.wait_for_selector(".ff-modal-preview .ff-preview-board")
    clip.hold(1.4)

    clip.click("#ff-modal-form input[name=title]", settle=0.2)
    page.keyboard.press("ControlOrMeta+A")
    clip.type("Big orders")
    clip.click("#ff-modal-form input[name=description]", settle=0.2)
    clip.type("Orders worth 150 to 450, the biggest first.", per_key=0.04)
    clip.hold(1.2)                                                    # the title follows

    columns = ".ff-list[data-name=columns]"
    page.locator(f"{columns} .ff-list-item[data-value=channel]").scroll_into_view_if_needed()
    clip.hold(0.6)
    clip.move(f"{columns} .ff-list-item[data-value=amount] .ff-list-grip")   # amount to the front
    page.mouse.down()
    clip.move(f"{columns} .ff-list-item[data-value=order_day] .ff-list-name", steps=18)
    page.mouse.up()
    clip.hold(1.6)
    clip.click(f"{columns} .ff-list-item[data-value=channel] .ff-list-del", settle=1.6)

    clip.click(".ff-modal-preview .fireflyer-filter-indicator summary", settle=3.0)  # it's live
    clip.click(".ff-modal-preview .fireflyer-filter-indicator summary", settle=0.4)
    clip.click("#ff-modal-form .ff-cancel", settle=0.8)


# --- filtering while viewing: a clip per kind of column ----------------------------
#
# Each on the demo path's "Orders overview" in view mode, typed into a chart's
# badge: pick the column, the operator, the values, then +. Viewer state only —
# nothing is saved, and each recording starts from a fresh page.

_BADGE = ".fireflyer-filter-indicator"
_ORDERS_KPI = f".fireflyer-dashboard-cell:has-text('Orders') {_BADGE}"
_ALL_ORDERS = f".fireflyer-dashboard-cell:has-text('All orders') {_BADGE}"


def _open(clip: Clip, badge: str, settle: float = 0.6) -> None:
    clip.tap(f"{badge} > summary", settle=settle)


def _column(clip: Clip, badge: str, column: str, op: str | None = None) -> None:
    """Pick `column` in the badge's bottom row (and `op`, when not `=`)."""
    form = f"{badge} form.fireflyer-filter-add"
    clip.tap(f"{form} .ff-colpick > summary", settle=0.5)
    item = clip.page.locator(f"{form} .ff-colpick-item[data-column='{column}']").first
    item.evaluate("e => e.scrollIntoView({block: 'nearest'})")     # within the list only
    clip.hold(0.3)
    clip.tap(f"{form} .ff-colpick-item[data-column='{column}']", settle=0.2)
    clip.page.wait_for_selector(f"{form} input[name=filter_column][value='{column}']", state="attached")
    clip.hold(0.6)
    if op:
        _operator(clip, form, op)


def _operator(clip: Clip, form: str, op: str) -> None:
    clip.move(f"{form} select[name=filter_op]")
    clip.page.locator(f"{form} select[name=filter_op]").first.select_option(op)
    clip.hold(0.8)


def _tick(clip: Clip, badge: str, *values: str) -> None:
    for value in values:
        clip.tap(f"{badge} form.fireflyer-filter-add label.ff-filter-choice:has(input[value='{value}'])", settle=0.3)


def _add(clip: Clip, badge: str) -> None:
    """+ (or ✓ for an edit): the dashboard redraws, the badge stays open."""
    clip.tap(f"{badge} form.fireflyer-filter-add button[type=submit]", settle=1.4)


def _look(clip: Clip, badge: str, seconds: float = 2.2) -> None:
    """Close the badge to see the dashboard it narrowed."""
    _open(clip, badge, settle=seconds)


def _edit_to_not_in(clip: Clip, badge: str) -> None:
    """The filter's pencil, `≠` for `=`, ✓: the rows the first one left out."""
    _open(clip, badge)
    clip.tap(f"{badge} .fireflyer-filter-edit", settle=0.8)
    _operator(clip, f"{badge} form.fireflyer-filter-add", "ni")
    _add(clip, badge)
    _look(clip, badge, 2.6)


def _all_orders(clip: Clip) -> None:
    """The Orders tab, scrolled to its every-column table (gift, tags shown)."""
    clip.tap(".fireflyer-tab[data-tab-name=Orders]", settle=1.4)
    clip.page.evaluate("""() => {
        const cell = [...document.querySelectorAll('.fireflyer-dashboard-cell')]
          .find(c => c.textContent.includes('All orders'));
        const pane = document.querySelector('.pane-body');
        pane.scrollTop += cell.getBoundingClientRect().top - 96;   // under the tab bar
    }""")
    clip.hold(1.0)


def filter_text(clip: Clip) -> None:
    """docs/use-cases.md → "Filter text": channel = partner, phone, then ≠."""
    clip.goto("/d/demo/orders-overview")
    clip.hold(1.2)
    _open(clip, _ORDERS_KPI)
    _column(clip, _ORDERS_KPI, "channel")
    _tick(clip, _ORDERS_KPI, "partner", "phone")
    _add(clip, _ORDERS_KPI)
    _look(clip, _ORDERS_KPI)
    _edit_to_not_in(clip, _ORDERS_KPI)


def filter_number(clip: Clip) -> None:
    """docs/use-cases.md → "Filter numbers": amount from 100 to 300, both included."""
    page = clip.page
    form = f"{_ORDERS_KPI} form.fireflyer-filter-add"
    clip.goto("/d/demo/orders-overview")
    clip.hold(1.2)
    _open(clip, _ORDERS_KPI)
    _column(clip, _ORDERS_KPI, "amount", "between")
    page.wait_for_selector(f"{form} input[name=filter_low]")
    clip.tap(f"{form} input[name=filter_low]", settle=0.2)
    clip.type("100", per_key=0.12)
    clip.tap(f"{form} input[name=filter_high]", settle=0.2)
    clip.type("300", per_key=0.12)
    clip.hold(1.0)                                                   # the slider follows
    _add(clip, _ORDERS_KPI)
    _look(clip, _ORDERS_KPI, 2.8)


def filter_date(clip: Clip) -> None:
    """docs/use-cases.md → "Filter dates": order_day over June's first two weeks,
    on the calendar (which opens on the data's latest month)."""
    page = clip.page
    form = f"{_ORDERS_KPI} form.fireflyer-filter-add"
    clip.goto("/d/demo/orders-overview")
    clip.hold(1.2)
    # Up a little, so the two-month calendar fits the clip's height.
    page.evaluate("document.querySelector('.pane-body').scrollTop = 40")
    clip.hold(0.4)
    _open(clip, _ORDERS_KPI)
    _column(clip, _ORDERS_KPI, "order_day", "between")
    page.wait_for_selector(f"{form} .ff-range > summary")
    clip.tap(f"{form} .ff-range > summary", settle=0.8)
    clip.tap(f"{form} .ff-cal-day[aria-label$='June 1, 2026']", settle=0.8)
    clip.tap(f"{form} .ff-cal-day[aria-label$='June 14, 2026']", settle=1.2)
    _add(clip, _ORDERS_KPI)
    _look(clip, _ORDERS_KPI, 3.0)                                    # the bars are those days


def filter_yes_no(clip: Clip) -> None:
    """docs/use-cases.md → "Filter yes / no": gift = true, in the every-column table."""
    clip.goto("/d/demo/orders-overview")
    clip.hold(1.0)
    _all_orders(clip)
    _open(clip, _ALL_ORDERS)
    _column(clip, _ALL_ORDERS, "gift")
    _tick(clip, _ALL_ORDERS, "true")
    _add(clip, _ALL_ORDERS)
    _look(clip, _ALL_ORDERS, 3.0)


def filter_list(clip: Clip) -> None:
    """docs/use-cases.md → "Filter a list": tags = promo (a list holding it),
    then ≠ (holding none — an order with no tags too)."""
    clip.goto("/d/demo/orders-overview")
    clip.hold(1.0)
    _all_orders(clip)
    _open(clip, _ALL_ORDERS)
    _column(clip, _ALL_ORDERS, "tags")
    _tick(clip, _ALL_ORDERS, "promo")
    _add(clip, _ALL_ORDERS)
    _look(clip, _ALL_ORDERS, 2.6)
    _edit_to_not_in(clip, _ALL_ORDERS)


def _fast(clip: Clip, text: str, chunk: int = 5) -> None:
    """Type `text` a few keys a frame: long text without a frame a key."""
    for i in range(0, len(text), chunk):
        clip.page.keyboard.type(text[i:i + chunk])
        clip.hold(0.06)


def _dialog(clip: Clip, button: str) -> None:
    """Open a list page's dialog from its head, the page put back where it was:
    at 960 px a list is wider than the clip, and the click scrolls it."""
    clip.click(button, settle=0.1)
    clip.page.evaluate("window.scrollTo(0, 0)")
    clip.hold(0.3)


def dataset_query(clip: Clip) -> None:
    """docs/use-cases.md → "Read a dataset from a database" (portal mode, the
    Docker compose's Postgres): a connection `server` to its `postgres`
    database — not Fireflyer's own, which only `internal` reads — then a dataset
    `databases` from a query on it, refreshed. Removes both first."""
    page, base = clip.page, clip.base
    page.request.post(f"{base}/datasets/_admin/databases/delete")
    page.request.post(f"{base}/connections/server/delete")

    clip.goto("/connections")
    clip.hold(1.0)
    _dialog(clip, "th.actions button[title='New connection']")
    _fast(clip, "server", 3)
    clip.click("#conn-form textarea[name=description]", settle=0.1)
    _fast(clip, "The database server's own catalog")
    clip.click("#conn-form input[name=url]", settle=0.1)
    _fast(clip, "postgresql+psycopg://fireflyer@postgres:5432/postgres", 8)
    clip.click("#conn-form input[name=password]", settle=0.1)
    _fast(clip, "fireflyer", 3)
    clip.click(".conn-modes button[data-mode=code]", settle=1.8)      # the same, as YAML
    clip.click("#conn-form button.ok", settle=0.8)
    page.evaluate("window.scrollTo(0, 0)")
    clip.click("tr:has-text('server') button[title='Test the connection']", settle=1.4)
    page.evaluate("window.scrollTo(0, 0)")

    clip.click(".ff-switch a[href^='/datasets']", settle=0.6)
    _dialog(clip, "th.actions button[title='New dataset']")
    clip.click("#upload-dialog .ds-kinds button[data-kind=sql]", settle=0.6)     # CSV | SQL
    clip.click("#upload-dialog input[name=name]", settle=0.1)
    _fast(clip, "databases", 3)
    clip.click("#upload-dialog textarea[name=description]", settle=0.1)
    _fast(clip, "Every database on the server, and its size")
    page.select_option("#upload-dialog select[name=connection]", "server")
    clip.hold(0.4)
    clip.click("#upload-dialog .ff-sql .cm-content", settle=0.1)      # a code editor
    _fast(clip, "select datname as database, datallowconn as open,\n  pg_database_size(datname) as bytes\n", 6)
    clip.page.keyboard.press("Shift+Tab")                # Enter kept the line's indent
    _fast(clip, "from pg_database", 6)
    clip.hold(0.8)
    clip.click("#upload-dialog button.ok", settle=1.5)
    page.reload()
    page.wait_for_load_state("networkidle")
    clip._dot()
    clip.hold(2.0)                                       # typed columns: a number, a yes/no
    clip.click("#ds-modes button[data-mode=edit]", settle=2.6)      # its SQL, under the columns
    clip.click("button[aria-label=Refresh]", settle=1.4)
    clip.click("#ds-history summary", settle=2.6)                   # the next version, from the query


def usage(clip: Clip) -> None:
    """docs/use-cases.md → "See how Fireflyer is used" (portal mode): the Fireflyer Usage
    dashboard in `_admin`, its activity, then its content. Refreshes the
    activity first, so it's up to date."""
    clip.page.request.post(f"{clip.base}/datasets/_admin/usage_activity/refresh")
    clip.goto("/d/_admin/Fireflyer%20Usage")
    clip.hold(3.0)
    clip.move(".fireflyer-tab[data-tab-name=Activity]")
    for top in (180, 360, 540, 800):                     # down the Activity tab, to the errors
        clip.page.evaluate(f"document.querySelector('.pane-body').scrollTo({{top: {top}, behavior: 'smooth'}})")
        clip.hold(1.8)
    clip.page.evaluate("document.querySelector('.pane-body').scrollTo({top: 0, behavior: 'smooth'})")
    clip.hold(0.6)
    clip.click(".fireflyer-tab[data-tab-name=Content]", settle=2.6)
    clip.page.evaluate("document.querySelector('.pane-body').scrollTo({top: 400, behavior: 'smooth'})")
    clip.hold(2.6)


SCENES = {
    "users-roles": users_roles, "groups": groups, "edit-chart": edit_chart,
    "filter-text": filter_text, "filter-number": filter_number, "filter-date": filter_date,
    "filter-yes-no": filter_yes_no, "filter-list": filter_list,
    "dataset-query": dataset_query, "usage": usage,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scene", choices=sorted(SCENES))
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # The portal's own certificate is self-signed.
        page = browser.new_page(viewport=SIZE, color_scheme="dark", ignore_https_errors=True)
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
