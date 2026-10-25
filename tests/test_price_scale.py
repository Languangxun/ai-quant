"""后复权价 → 实际价 换算测试 (浦发 hfq 164.87 vs 实际 ~9.5)。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_display_price_scales_hfq_to_real():
    from data.stock import cli_bridge as b
    # 无 adjust 记录时按 1.0 处理
    assert b.display_price("sh999999", 10.0) == 10.0
    # 有 adjust 记录时按 k 缩放
    import sqlite3
    con = sqlite3.connect(b.DB_PATH, timeout=30)
    try:
        r = con.execute(
            "SELECT k FROM adjust WHERE code=?", ("sh600000",)).fetchone()
    finally:
        con.close()
    if not r:
        return
    k = r[0]
    assert abs(b.display_price("sh600000", 164.87) - 164.87 * k) < 1e-6
    # 实际价应落在合理区间（浦发 9~10 元）
    real = b.display_price("sh600000", 164.87)
    assert 8.0 < real < 11.0, f"浦发实际价 {real} 不合理"


def test_adjust_k_default_one():
    from data.stock import cli_bridge as b
    assert b.adjust_k("sh999999") == 1.0
