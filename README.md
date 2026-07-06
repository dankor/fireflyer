# Fireflyer

> _Fire, walk with me._

Fireflyer is a new generation BI tool built for engineers, AI agents and regular users. It's **low-code**, **modular**, and **local-first**. 

> ⚠️ **Status:** early MVP, under heavy development, not production-ready. The goal is to make BI easy for everyone.

Today it turns a CSV into charts (tables, pie, bar, and more), YAML dashboards, and crossfiltering, with a browser editor for live resize and an AI assistant. Deliberately out of scope for now: auth, multi-user, SQL/warehouses, joins, and large-dataset optimization.

---

## Three ways to build a dashboard

One engine, three ways in. Pick whichever fits you — and switch anytime. Whatever you build, it's the same dashboard underneath, saved as clean YAML you can commit.

### 🛠️ Write it in code

For engineers. Describe a whole dashboard in a single YAML file.

**Why it's good:** your dashboards live in your editor and in git — you review them like any other code, drop them into a Jupyter notebook, and script them for reports that run themselves. No clicking through menus, no BI server to babysit.

[yaml-edit.webm](https://github.com/user-attachments/assets/772943e5-f59d-4cf5-b74d-8dc1c220f5be)

### 🖱️ Build it in the UI

For everyone. Open the browser editor and build by pointing and clicking: add charts from a menu, fill in a short form, drag rows and columns to resize, click a pie slice to filter the rest.

**Why it's good:** there's nothing to memorize and you see every change instantly. People who don't write code get a real dashboard on their own — and each edit is written back as clean YAML, so the visual and code views never fall out of sync.

[ui-edit.webm](https://github.com/user-attachments/assets/5e0ebd86-2a30-4eed-907c-bde191415a4a)

### 🤖 Ask the AI

For anyone in a hurry. The built-in chat rewrites your dashboard from plain-English requests — *"add a table of orders,"* *"move the pie chart next to the table,"* *"make the top row taller."*

**Why it's good:** you describe what you want instead of building it. The assistant checks every change before applying it, and since it writes the same YAML, you can keep editing in code or the UI afterward.

[ai-edit.webm](https://github.com/user-attachments/assets/c3360915-7e6c-4c61-8554-e4240c98a830)

---

## Quickstart

### Run with Docker (recommended)

The fastest way to get the editor running — no Python setup needed:

```bash
git clone https://github.com/dankor/fireflyer.git
cd fireflyer
docker compose up --build
```

Open <http://127.0.0.1:8000>. You get a two-pane editor: write a dashboard YAML on the left, hit **Run**, and see it render on the right. Toggle **Hide YAML** for a view-only mode.

Source and `files/` are mounted into the container with `--reload`, so your edits hot-reload live. Drop your own CSVs into `files/` and reference them in a dashboard. Stop with `Ctrl-C` (or `docker compose down`).

### Run locally with Python

Requires **Python ≥ 3.11**.

```bash
git clone https://github.com/dankor/fireflyer.git
cd fireflyer
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
python -m fireflyer.web        # editor at http://127.0.0.1:8000
```

### Use it as a library

```python
import fireflyer as ff

chart = ff.chart.table(dataset="files/orders.csv", title="Orders")
chart  # renders inline in Jupyter via _repr_html_

# or grab the HTML string
html = chart.to_html()
```

Polars does the data work under the hood — you never touch it directly.

---

## Charts

A handful of chart types — tables, pie, bar, and more. Every chart takes a `dataset` and a `title`; the rest is a couple of chart-specific fields.

```python
ff.chart.table(dataset="files/orders.csv", title="Orders")
ff.chart.pie(dataset="files/orders.csv", title="Orders by Status", column="status")
ff.chart.bar(dataset="files/orders.csv", title="Orders by Day", x="day", y="status")
```

Each chart's full options live in its spec: [`fireflyer/chart/<name>/spec.md`](fireflyer/chart).

---

## Dashboards

A dashboard is **one YAML file** that declares its datasets, its charts, and how they lay out on a page:

```yaml
datasets:
  orders:
    path: files/orders.csv

charts:
  orders_table:
    type: table
    dataset: orders
    title: Orders

  status_pie:
    type: pie
    dataset: orders
    title: Orders by Status
    column: status

dashboard:
  - Overview                                    # a header
  - ["@40", "orders_table:60", "status_pie:40"] # a row: @height, then chart:width (proportion)
  - "-"                                         # a separator
  - ["@30", "orders_table:100"]
```

Rows read as `["@<height>", "<chart>:<width>", ...]`, where widths are simple proportions — `1:1:1` is equal thirds. Render it with `ff.Dashboard.from_yaml(text).to_html()`, or just paste it into the web editor.

**Crossfiltering** comes for free: click a pie slice and every other chart narrows to match — no page reload. You can also declare fixed `filters` on any chart.

The full layout DSL, filter model, and editor behavior are specified in **[`architecture.md`](architecture.md)**.

---

## AI assistant

The editor's built-in chat edits the dashboard for you — ask in plain language and it rewrites the YAML, updates the preview, and explains what changed. It runs on Claude; add a key to turn it on:

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

Without a key the editor still works fully — the chat panel just shows a setup notice. The key stays server-side and is never sent to the browser.

---

## Development

```bash
pip install -e ".[test]"
pytest                       # run the snapshot suite
UPDATE_SNAPSHOTS=1 pytest    # regenerate snapshots after an intentional change

# or in the container, no local Python needed:
docker compose run --rm fireflyer pytest
```

Tests are **snapshot-based**: each pairs an input CSV + chart/dashboard definition with the exact expected HTML in `tests/snapshots/`. If you change rendering on purpose, regenerate and review the diff.

Contributions welcome. Keep the code approachable: small functions, clear names, comments that explain *why*. See [`CLAUDE.md`](CLAUDE.md) for the conventions this repo follows (they apply to humans and agents alike).

Release notes live in [`CHANGELOG.md`](CHANGELOG.md).

---

## License

**Apache License 2.0 with the [Commons Clause](https://commonsclause.com/)** — see [`LICENSE`](LICENSE).

This is *source-available*, not OSI open source. In short:

- ✅ **Free** to use, modify, self-host internally, study, and build on — for individuals and companies alike.
- ❌ **Not** for **selling** the software, or offering it (or a derivative) as a **paid or hosted/cloud service (SaaS)**, or charging fees whose value derives substantially from it.

Want to sell it or run it as a commercial/cloud service? Get a commercial license — contact **Danylo Korostil** (<ted.korostiled@gmail.com>).
