"""比分牌**自动识别**：定位（多候选区域扫描）+ 解析（时钟剔除）+ 可信度校验。

用户原话："比分牌子你不能自动识别吗，表比分牌不太好吧"
—— 手动框选是退步，自动识别要能真正跑通。

本文件的夹具全部来自**实测 dump 的真实 OCR 词块**（见下），不是编的：
  t=0   画面真值 `FAIRFIELD 0  SYCAMORE 0  7:40 | 1st`
        OCR 吐出：'ycg_YORE'(队名被读坏) '0'(x2430) '7'(x2559) '40'(x2623)
                  '|'(x2747) '1'(x2791) 'st'
  t=120 画面真值 `FAIRFIELD 5  SYCAMORE 5  5:46 | 1st`
        OCR 吐出：'FA!B!IEL!' '5'(x1590) '5'(x2429) '5'(x2560) '46'(x2623)
                  '|' '1' 'st'

两个根因都是**实测确认**的：
  1. 时钟 `7:40` 被 OCR 拆成两个数字 `7` 和 `40`，被当成队名旁边的比分
     → 真值 0:0 被读成 **0:7**；
  2. 节次 `1`（在 `|` 右边）也被当成数字。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop.ocr_scoreboard import (  # noqa: E402
    _auto_candidates, _drop_clock_and_period, _parse_pairs, _parse_words,
    _reading_quality)

# ---- 实测 dump 的词块（t=0）----
WORDS_T0 = [
    {"x": 1984.0, "y": 352.0, "w": 384.0, "h": 89.0, "text": "ycg_YORE"},
    {"x": 2430.0, "y": 358.0, "w": 56.0, "h": 74.0, "text": "0"},
    {"x": 2559.0, "y": 358.0, "w": 41.0, "h": 72.0, "text": "7"},
    {"x": 2623.0, "y": 358.0, "w": 85.0, "h": 72.0, "text": "40"},
    {"x": 2747.0, "y": 352.0, "w": 10.0, "h": 88.0, "text": "|"},
    {"x": 2791.0, "y": 357.0, "w": 23.0, "h": 73.0, "text": "1"},
    {"x": 2828.0, "y": 361.0, "w": 57.0, "h": 69.0, "text": "st"},
]
# ---- 实测 dump 的词块（t=120）----
WORDS_T120 = [
    {"x": 1217.0, "y": 352.0, "w": 327.0, "h": 89.0, "text": "FA!B!IEL!"},
    {"x": 1590.0, "y": 359.0, "w": 57.0, "h": 72.0, "text": "5"},
    {"x": 2429.0, "y": 358.0, "w": 57.0, "h": 73.0, "text": "5"},
    {"x": 2560.0, "y": 358.0, "w": 40.0, "h": 72.0, "text": "5"},
    {"x": 2623.0, "y": 358.0, "w": 86.0, "h": 72.0, "text": "46"},
    {"x": 2746.0, "y": 352.0, "w": 11.0, "h": 89.0, "text": "|"},
    {"x": 2791.0, "y": 357.0, "w": 24.0, "h": 73.0, "text": "1"},
    {"x": 2828.0, "y": 360.0, "w": 57.0, "h": 70.0, "text": "st"},
]


def _nums(words):
    return [(float(w["x"]), float(w["y"]), int(w["text"]), float(w["w"]),
             float(w["h"])) for w in words if str(w["text"]).isdigit()]


def test_clock_digits_are_dropped():
    """时钟 `5:46` 的两个数字必须被剔掉 —— 否则会被当成客队比分。

    判据：时钟永远是**两个紧挨着的数字**（实测间隙 23px，而字高 72px）。
    """
    kept = _drop_clock_and_period(_nums(WORDS_T120), WORDS_T120)
    vals = [k[2] for k in kept]
    assert 46 not in vals, "时钟的 46 没被剔掉：%s" % vals
    assert vals.count(5) == 2, "应当只剩两队的 5 和 5：%s" % vals


def test_period_after_separator_is_dropped():
    """`|` 右边的节次（1st 的 1）必须被剔掉。

    ⚠️ 踩过的坑：`_clean_token` 会 strip 掉 `|`，所以比对分隔符必须用**原始**文本，
    否则这条判断永远不生效（实测节次一直被当成比分）。
    """
    for words in (WORDS_T0, WORDS_T120):
        kept = _drop_clock_and_period(_nums(words), words)
        xs = [k[0] for k in kept]
        assert all(x < 2747.0 for x in xs), \
            "`|` 右边的数字没被剔掉：%s" % xs


def test_t120_parses_to_true_score():
    """t=120 真值 5:5 —— 必须解出 5:5（修复前是 0:7 之类的噪声）。"""
    got = _parse_pairs(WORDS_T120)
    assert got is not None, "应当能解出比分"
    assert (got["home"], got["away"]) == (5, 5), \
        "应当解出 5:5（画面真值），实际 %s:%s" % (got["home"], got["away"])


def test_t0_no_longer_reports_the_clock_as_a_score():
    """t=0 真值 0:0：修好之后**宁可解不出，也不能报 0:7**。

    这一帧 OCR 把 FAIRFIELD 的 0 并进了队名词块，只剩一个数字 ——
    数据不足时返回 None 是**正确**行为（报错比分比读不出来更糟）。
    """
    got = _parse_pairs(WORDS_T0)
    if got is not None:
        assert (got["home"], got["away"]) != (0, 7), \
            "又把时钟的 7 当成客队比分了"
        assert got["away"] != 40, "把时钟的 40 当成了比分"


def test_quality_accepts_a_real_score_sequence():
    """真实序列：比分很少变、只增不减 → 可接受。"""
    class R:
        def __init__(self, t, h, a):
            self.t, self.home, self.away = t, h, a
    seq = [R(0, 5, 5), R(30, 5, 5), R(60, 5, 5), R(90, 5, 8)]
    q, ok, why = _reading_quality(seq, 5, {"home": "A", "away": "B"})
    assert ok, "真实序列应被接受：q=%.2f why=%s" % (q, why)


def test_quality_rejects_frame_by_frame_noise():
    """噪声序列：每帧都不一样、比分还会回退 → 必须拒。"""
    class R:
        def __init__(self, t, h, a):
            self.t, self.home, self.away = t, h, a
    noise = [R(0, 0, 7), R(30, 2, 6), R(60, 5, 5)]     # 实测的噪声读数
    q, ok, why = _reading_quality(noise, 4, {"home": "X", "away": "X"})
    assert not ok, "噪声被接受了（q=%.2f）：%s" % (q, why)


def test_quality_does_not_veto_on_team_name_collision():
    """队名读重了**只降分、不否决** —— 用户要的是比分。

    实测教训：低清素材上队名经常读坏（两边都读成 FAIRFIELD），
    但比分 `5:5 ×3` 完全正确且一致。我第一版把"队名相同"当成了否决条件，
    于是把正确比分丢掉了。
    """
    class R:
        def __init__(self, t, h, a):
            self.t, self.home, self.away = t, h, a
    seq = [R(0, 5, 5), R(30, 5, 5), R(60, 5, 5)]
    q, ok, why = _reading_quality(seq, 4, {"home": "FAIRFIELD",
                                           "away": "FAIRFIELD"})
    assert ok, "队名读重不该否决正确比分：q=%.2f why=%s" % (q, why)
    assert "队名" in why, "应当在说明里注明队名读重了"


def test_auto_candidates_cover_both_rows_and_corners():
    """候选区域要覆盖上下两排 + 左中右 —— 台标位置因台而异。"""
    cands = _auto_candidates(1280, 720)
    names = [c[0] for c in cands]
    assert len(cands) >= 6, "候选太少，自动定位会经常失败：%s" % names
    assert any("下方" in n for n in names), "必须有下排候选"
    assert any("上方" in n or "中上" in n for n in names), "必须有上排候选"
    for _n, nx, ny, nw, nh in cands:
        assert 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0
        assert 0.0 < nw <= 1.0 and 0.0 < nh <= 1.0
        assert nx + nw <= 1.0001 and ny + nh <= 1.0001, \
            "候选区域超出画面：%s" % _n
