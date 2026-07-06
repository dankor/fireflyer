"""Guard: every chart's editor PARAMS stay in sync with its constructor.

If someone adds a constructor field but forgets to declare a Param (or vice
versa), the edit modal would silently omit or invent a config key. This test
fails the moment the two drift apart.
"""

from dataclasses import fields

from fireflyer.dashboard import CHART_TYPES


def test_every_chart_declares_params():
    for name, cls in CHART_TYPES.items():
        assert hasattr(cls, "PARAMS"), f"{name} chart has no PARAMS"


def test_params_cover_exactly_the_constructor_fields():
    for name, cls in CHART_TYPES.items():
        ctor = {f.name for f in fields(cls)}
        declared = {p.name for p in cls.PARAMS}
        assert declared == ctor, (
            f"{name} chart: PARAMS {sorted(declared)} != constructor "
            f"fields {sorted(ctor)}"
        )
