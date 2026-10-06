import strategies  # noqa: F401  (自注册)
from strategies import registry


def test_registry_has_builtin():
    names = registry.names()
    for want in ("chaodi_trend", "morphology_tier", "lgbm_prob",
                 "mix_ensemble"):
        assert want in names, f"缺策略 {want}, 现有 {names}"


def test_chaodi_trend_smoke():
    from core.types import Bar
    strat = registry.get("chaodi_trend")
    bars = [Bar(code="sh600000", date=f"2026-09-{d:02d}",
                open=10, high=10.5, low=9.8, close=10 + d * 0.05,
                vol=100 + d)
            for d in range(1, 28)]
    # 追加突破段: 60日新高 + 放量
    bars += [Bar(code="sh600000", date="2026-10-05",
                 open=11, high=12, low=10.9, close=11.9, vol=500),
             Bar(code="sh600000", date="2026-10-06",
                 open=11.9, high=13, low=11.8, close=12.8, vol=600)]
    # 补足 65 根
    pre = [Bar(code="sh600000", date=f"2026-06-{d:02d}",
               open=9, high=9.5, low=8.8, close=9.2, vol=80)
           for d in range(1, 29)]
    bars = pre + bars
    sigs = strat.generate({"sh600000": bars}, {"date": "2026-10-06"})
    assert isinstance(sigs, list)


def test_mix_ensemble_no_crash_without_ml():
    from core.types import Bar
    strat = registry.get("mix_ensemble")
    bars = [Bar(code="sh600000", date=f"2026-09-{d:02d}",
                open=10, high=10.2, low=9.8, close=10.0, vol=100)
            for d in range(1, 28)]
    sigs = strat.generate({"sh600000": bars}, {"date": "2026-09-27"})
    assert isinstance(sigs, list)
