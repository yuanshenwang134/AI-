"""篮下视觉判进球（hoopsight）的自检。

这些用例刻意**不依赖任何视频素材**：判据全是几何/时序，
喂进构造出来的「候选块时序」就能断言「判成进球 / 判成没进」。
这样在没素材、没显卡的机器上也能守住这条路线的正确性。
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from aihoop.hoop import Hoop                      # noqa: E402
from aihoop.hoopsight import (SightConfig, _expect_ball_px,   # noqa: E402
                              _judge_span, _person_blobs, _pick_blob,
                              _window, _shots_from_blob_series)

HOOP = Hoop(cx=100.0, cy=60.0, rx=40.0, ry=18.0, votes=10,
            confidence=0.9, method="test")
CFG = SightConfig()


def _blob(t, cx, cy, area):
    return (round(t, 3), cx, cy, area)


# 球的期望尺寸（像素面积），用于构造「球的尺寸」的块
_EXP_D = _expect_ball_px(HOOP, CFG)
_EXP_A = math.pi * (_EXP_D / 2.0) ** 2


def test_window_covers_rim_and_net():
    x0, y0, x1, y1 = _window(CFG, HOOP)
    assert x0 < HOOP.cx - HOOP.rx and x1 > HOOP.cx + HOOP.rx
    assert y0 < HOOP.cy and y1 > HOOP.cy + HOOP.ry * 1.5


def test_expect_ball_size_uses_rim():
    # 球直径 ≈ 0.55 × 篮圈直径
    assert abs(_EXP_D - HOOP.rx * 2 * CFG.ball_d_ratio) < 1e-6


def test_made_basket_is_detected():
    """从筐上方直落筐下：应判成进球。"""
    fy = 1.0 / 24.0
    series = []
    ys = [10, 20, 30, 42, 58, 62, 80, 100, 115]      # 穿越 cy=60 → 落到 115
    for i, y in enumerate(ys):
        series.append(_blob(10.0 + i * fy, 100.0, y, _EXP_A))
    shots = _shots_from_blob_series(series, HOOP, CFG, fps=24.0)
    assert len(shots) == 1, shots
    s = shots[0]
    assert s.made and s.t >= 10.0
    assert s.drop_px > HOOP.ry * CFG.min_drop_ry
    assert abs(s.cross_x - HOOP.cx) < HOOP.rx * CFG.cross_inner
    assert 0.3 <= s.confidence <= 0.97


def test_rise_never_crossing_is_not_a_shot():
    """球从筐下升到筐上（篮板球的反弹）不算进球。"""
    fy = 1.0 / 24.0
    series = [_blob(5.0 + i * fy, 100.0, y, _EXP_A)
              for i, y in enumerate([120, 100, 80, 60, 40, 20, 10])]
    assert _shots_from_blob_series(series, HOOP, CFG, fps=24.0) == []


def test_pass_beside_rim_is_not_a_shot():
    """横向从筐外掠过（全程在篮圈右侧外）：不算进球。

    注意构造方式：必须**整段**都在篮圈外。
    只把前几个点放到圈外、后面漂回圈内，几何上那真的是穿筐，
    模块判成进球是对的（曾用这种数据写测试，反而把自己的正确定判成了错）。
    """
    fy = 1.0 / 24.0
    xs = [170, 172, 175, 178, 182]     # cx=100, rx=40 → 全在圈外
    ys = [10, 30, 55, 70, 95]
    series = [_blob(3.0 + i * fy, xs[i], ys[i], _EXP_A) for i in range(5)]
    assert _shots_from_blob_series(series, HOOP, CFG, fps=24.0) == []


def test_crossing_outside_rim_is_not_a_shot():
    """从筐外侧（横向偏出篮圈）落下去：下落再干脆也不算进球。"""
    fy = 1.0 / 24.0
    xs = [150, 160, 170, 178, 185]
    ys = [20, 40, 60, 80, 100]
    series = [_blob(7.0 + i * fy, xs[i], ys[i], _EXP_A) for i in range(5)]
    assert _shots_from_blob_series(series, HOOP, CFG, fps=24.0) == []


def test_too_few_evidence_frames_rejected():
    fy = 1.0 / 24.0
    series = [_blob(1.0, 100.0, 20.0, _EXP_A), _blob(1.0 + fy, 100.0, 90.0, _EXP_A)]
    assert _shots_from_blob_series(series, HOOP, CFG, fps=24.0) == []


def test_two_baskets_in_one_clip_are_split():
    fy = 1.0 / 24.0
    series = []
    for base in (2.0, 40.0):
        for i, y in enumerate([15, 30, 55, 70, 100]):
            series.append(_blob(base + i * fy, 100.0, y, _EXP_A))
    shots = _shots_from_blob_series(series, HOOP, CFG, fps=24.0)
    assert len(shots) == 2, shots
    assert shots[0].t < shots[1].t


def test_chain_requires_monotone_descent():
    """球砸在筐上弹回、**始终没落到篮圈下方**：不算进球。"""
    fy = 1.0 / 24.0
    ys = [10, 40, 60, 70, 25, 20, 18]      # 到 70（筐口，仍在 cy+ry=78 之上）就弹回
    series = [_blob(1.0 + i * fy, 100.0, y, _EXP_A) for i, y in enumerate(ys)]
    shots = _shots_from_blob_series(series, HOOP, CFG, fps=24.0)
    assert shots == [], [s.to_dict() for s in shots]


def test_double_pump_is_split_into_separate_segments():
    """「落下去 → 弹回筐上 → 再落下去」= 两次触筐，不该拼成一次。"""
    fy = 1.0 / 24.0
    ys = [10, 40, 70, 100, 50, 65, 100, 95]
    series = [_blob(1.0 + i * fy, 100.0, y, _EXP_A) for i, y in enumerate(ys)]
    shots = _shots_from_blob_series(series, HOOP, CFG, fps=24.0)
    assert len(shots) == 2, [s.to_dict() for s in shots]
    assert shots[1].t_enter > shots[0].t


def test_small_jitter_during_descent_is_still_a_shot():
    """下落过程中抖一两像素（噪声）不该把进球判掉。"""
    fy = 1.0 / 24.0
    ys = [10, 32, 58, 76, 74, 100, 118]
    series = [_blob(1.0 + i * fy, 100.0, y, _EXP_A) for i, y in enumerate(ys)]
    assert len(_shots_from_blob_series(series, HOOP, CFG, fps=24.0)) == 1


def test_pick_blob_prefers_ball_sized_block_near_rim():
    near = (104.0, 62.0, _EXP_A, 12, 12)          # 筐口，尺寸正确
    big = (150.0, 70.0, _EXP_A * 6, 40, 40)       # 又大又偏（球衣/观众）
    picked = _pick_blob([big, near], HOOP, CFG)
    assert picked == near


def test_shot_dict_is_json_safe():
    import json
    fy = 1.0 / 24.0
    series = [_blob(1.0 + i * fy, 100.0, y, _EXP_A)
              for i, y in enumerate([10, 30, 55, 75, 100])]
    s = _shots_from_blob_series(series, HOOP, CFG, fps=24.0)[0]
    json.dumps(s.to_dict())


def test_person_blob_detected_under_rim():
    """筐下真有一个人形色块时，必须检出来并读到它的球衣色。

    这条用**合成画面**验证检测器本身是好的 —— 因为实拍素材里可能一个人
    都没有（实测 nybo 那段就是 0 个），那种情况下「0 个」是素材性质，
    不是检测器坏了。有了这个用例，「0 个」才有说服力。
    """
    np, cv2 = _np_cv()
    H, W = 400, 500
    frame = np.zeros((H, W, 3), np.uint8)
    frame[:] = (90, 90, 90)
    bg = frame.copy()
    hoop = Hoop(cx=120.0, cy=60.0, rx=57.5, ry=24.2, votes=10,
                confidence=0.9, method="t")
    # 筐下站一个人：高 140、宽 45（高/宽=3.1），上衣黄、裤子深
    x, y, w, h = 95, 120, 45, 140
    frame[y:y + int(h * 0.5), x:x + w] = (0, 210, 240)      # 黄（BGR）
    frame[y + int(h * 0.5):y + h, x:x + w] = (40, 40, 40)   # 深色裤子
    blobs = _person_blobs(cv2, np, frame, bg,
                          cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), hoop, CFG)
    assert len(blobs) == 1, [b.to_dict() for b in blobs]
    b = blobs[0]
    assert b.h >= CFG.people_min_h_px and b.aspect >= CFG.people_min_aspect
    assert b.colour_name == "黄", b.to_dict()


def test_person_blob_ignores_ball_and_net_sized_blocks():
    """球 + 网的连体块（方、小）不能被当成人。"""
    np, cv2 = _np_cv()
    H, W = 400, 500
    frame = np.zeros((H, W, 3), np.uint8)
    frame[:] = (90, 90, 90)
    bg = frame.copy()
    hoop = Hoop(cx=120.0, cy=60.0, rx=57.5, ry=24.2, votes=10,
                confidence=0.9, method="t")
    frame[60:115, 100:155] = (200, 200, 60)      # 55x55 的方块（球+网）
    blobs = _person_blobs(cv2, np, frame, bg,
                          cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), hoop, CFG)
    assert blobs == [], [b.to_dict() for b in blobs]


def test_person_blob_ignores_far_away_person():
    """离篮筐太远的人不算「筐下的人」。"""
    np, cv2 = _np_cv()
    H, W = 400, 500
    frame = np.zeros((H, W, 3), np.uint8)
    frame[:] = (90, 90, 90)
    bg = frame.copy()
    hoop = Hoop(cx=120.0, cy=60.0, rx=57.5, ry=24.2, votes=10,
                confidence=0.9, method="t")
    frame[330:400, 440:475] = (0, 210, 240)      # 右下角的人，离筐很远
    blobs = _person_blobs(cv2, np, frame, bg,
                          cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), hoop, CFG)
    assert blobs == [], [b.to_dict() for b in blobs]


def test_person_blob_ignores_text_like_strokes():
    """文字/字幕那种「细笔画」形状不能被当成人（填充率太低）。"""
    np, cv2 = _np_cv()
    H, W = 400, 500
    frame = np.zeros((H, W, 3), np.uint8)
    frame[:] = (90, 90, 90)
    bg = frame.copy()
    hoop = Hoop(cx=120.0, cy=60.0, rx=57.5, ry=24.2, votes=10,
                confidence=0.9, method="t")
    # 模拟烧进画面的字幕：一根竖笔画 + 一根横笔画，外接框很大但填充率很低
    frame[130:250, 105:112] = (240, 200, 0)
    frame[130:137, 105:165] = (240, 200, 0)
    blobs = _person_blobs(cv2, np, frame, bg,
                          cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), hoop, CFG)
    assert blobs == [], [b.to_dict() for b in blobs]


def _np_cv():
    from aihoop.hoopsight import _require_cv
    cv2, np = _require_cv()
    return np, cv2


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"[ok]   {name}")
        except AssertionError as e:
            fails += 1
            print(f"[FAIL] {name}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"[ERR]  {name}: {type(e).__name__}: {e}")
    print("hoopsight tests:", "all passed" if not fails else f"{fails} failed")
    raise SystemExit(1 if fails else 0)
