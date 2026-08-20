"""Regenerate the bundled `orders` sample — `files/orders.csv` + `.parquet`.

The sample exists to make the starter dashboard (`DEFAULT_YAML` in
`fireflyer/web/app.py`) a working tour of every feature, so its shape is chosen
deliberately:

* **10 weeks of dates, all in one year** — enough for the bar chart's grain
  picker to mean something (D / W / M all differ) without spanning a year
  boundary, which some tests rely on.
* **8 channels** — more than one legend page, so the pie and bar pagers have
  something to page.
* **A nullable `segment`** — so grouping on a column with missing values is
  visible in the demo rather than a surprise later.
* **`qty` + `unit_price` alongside `amount`** — lets a calc show a row-level
  formula (`unit_price * qty`) that agrees with the stored column.

Deterministic (fixed seed) so regenerating doesn't churn the file. Run from the
repo root:

    python files/make_orders.py
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

import polars as pl

ROWS = 120
SEED = 20260601
START = date(2026, 4, 6)          # a Monday, so weekly buckets line up
DAYS = 70                          # 10 weeks

# Weighted so the pie has a clear order rather than five equal slices.
STATUSES = [("paid", 34), ("shipped", 26), ("pending", 18), ("refunded", 12),
            ("cancelled", 10)]
# Eight, deliberately: the legend pages at six.
CHANNELS = ["web", "mobile app", "phone", "partner", "marketplace", "retail",
            "wholesale", "referral"]
# `None` is ~1 in 8 — enough to show up in a grouped table without dominating.
SEGMENTS = ["consumer", "business", "enterprise", None]
# Kyiv, roughly.
CENTRE_LAT, CENTRE_LNG = 50.4501, 30.5234


def _weighted(rng, pairs):
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights, k=1)[0]


def build() -> list[dict]:
    rng = random.Random(SEED)
    rows = []
    for i in range(1, ROWS + 1):
        qty = rng.choice([1, 1, 1, 2, 2, 3, 4, 5])
        unit_price = round(rng.uniform(4.99, 89.99), 2)
        rows.append({
            "id": i,
            "day": (START + timedelta(days=rng.randrange(DAYS))).isoformat(),
            "status": _weighted(rng, STATUSES),
            "channel": rng.choice(CHANNELS),
            "segment": rng.choice(SEGMENTS),
            "qty": qty,
            "unit_price": unit_price,
            "amount": round(qty * unit_price, 2),
            "lat": round(rng.gauss(CENTRE_LAT, 0.045), 6),
            "lng": round(rng.gauss(CENTRE_LNG, 0.065), 6),
        })
    rows.sort(key=lambda r: (r["day"], r["id"]))
    for n, row in enumerate(rows, 1):        # renumber so ids read in date order
        row["id"] = n
    return rows


def main() -> None:
    rows = build()
    out = Path(__file__).parent
    with (out / "orders.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    pl.read_csv(out / "orders.csv").write_parquet(out / "orders.parquet")

    frame = pl.read_csv(out / "orders.csv")
    print(f"{frame.height} rows -> {out/'orders.csv'} and .parquet")
    print(f"  {frame['day'].min()} .. {frame['day'].max()}")
    print(f"  statuses={frame['status'].n_unique()} "
          f"channels={frame['channel'].n_unique()} "
          f"segment nulls={frame['segment'].null_count()}")


if __name__ == "__main__":
    main()
