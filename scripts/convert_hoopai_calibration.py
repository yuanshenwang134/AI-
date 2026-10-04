"""把 HoopAI（本机旧项目）的球场标定转成 aihoop（队友项目）能读的 calibration.json。

两件事必须处理，否则转出来的标定是"看起来对、其实错"的：

1. **原点不同，差 1.575 m。**
   HoopAI : 原点在**篮筐地面点**（hoop_ground = (0,0)），底线 y = -1.575，罚球线 y = +4.225
   aihoop : 原点在**底线**（|y| = 0 是底线），篮筐 |y| = 1.575，中圈 |y| = 14
   → y_aihoop = y_hoopai + 1.575（x 相同）
   漏掉它会把篮下上篮算到离筐 1.575 m 之外，再叠加三分判断就会把上篮记成三分，**且不报错**。

2. **aihoop 的单应求解器只支持恰好 4 个点**（5 个会 IndexError）。
   而 HoopAI 的标定可以存 5~9 个点，且**存下来的点不一定自洽**
   （实测某份文件 5 点最小二乘残差 0.27~0.45 m）。所以这里从所有点里
   **穷举最优 4 点组合**，用 aihoop 自己的求解器重拟合，把残差与"篮筐落点"一起报出来。

用法：
    python scripts\\convert_hoopai_calibration.py <旧项目 court_calibration.json> \\
        [-o data\\calibration_xxx.json] [--for-video 视频文件名]
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

Y_SHIFT = 1.575           # 篮筐圆心距底线（FIBA）= 两套原点的差值
HALF_LANE = 2.45
COURT_HALF_LEN = 14.0
EXPECT_RANGES = {          # 转换后该落在哪（米）
    "lane_left_baseline": (-HALF_LANE, 0.0),
    "lane_right_baseline": (HALF_LANE, 0.0),
    "hoop_ground": (0.0, Y_SHIFT),
}


def apply_h(H, x, y):
    v = [H[0][0] * x + H[0][1] * y + H[0][2],
         H[1][0] * x + H[1][1] * y + H[1][2],
         H[2][0] * x + H[2][1] * y + H[2][2]]
    if abs(v[2]) < 1e-12:
        raise ZeroDivisionError("单应矩阵退化")
    return v[0] / v[2], v[1] / v[2]


def safe_apply_h(H, x, y):
    """投不动（退化 / 数值爆炸）时返回 None，而不是抛异常。

    4 点组合里有的画法会让矩阵退化（w≈0），这种组合必须被判负，
    而不是让整支脚本挂掉——实测踩过。
    """
    try:
        gx, gy = apply_h(H, x, y)
    except (ZeroDivisionError, IndexError, TypeError, ValueError):
        return None
    if not (math.isfinite(gx) and math.isfinite(gy)) or abs(gx) > 1e4 or abs(gy) > 1e4:
        return None
    return gx, gy


def _invert(H):
    """3x3 求逆（纯 python，避免为一个自检引入 numpy）。"""
    a, b, c = H[0]
    d, e, f = H[1]
    g, h, i = H[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-15:
        raise ZeroDivisionError("单应矩阵不可逆")
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default="", help="默认写到 data/calibration_from_hoopai.json")
    ap.add_argument("--for-video", default="")
    ap.add_argument("--min-spread-m", type=float, default=4.0,
                    help="4 点之间的最小包围盒边长（米），太小则外推误差大")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_file():
        print(f"找不到：{src}")
        return 2
    old = json.loads(src.read_text(encoding="utf-8"))
    pts = []
    for p in old.get("points") or []:
        img, court = p.get("image"), p.get("court")
        if img and court and len(img) == 2 and len(court) == 2:
            pts.append({"name": p.get("name", "?"),
                        "px": (float(img[0]), float(img[1])),
                        "m": (float(court[0]), float(court[1]) + Y_SHIFT)})   # ← 唯一的换算
    if len(pts) < 4:
        print(f"有效点只有 {len(pts)} 个（需要 ≥4）")
        return 2
    print(f"源文件：{src}")
    print(f"读出 {len(pts)} 个点，已按 y += {Y_SHIFT} 换到 aihoop 坐标系")
    for p in pts:
        print(f"  {p['name']:24} 像素({p['px'][0]:7.1f},{p['px'][1]:7.1f})  球场({p['m'][0]:7.3f},{p['m'][1]:7.3f})")

    # ---- 穷举 4 点组合：4 点单应必然精确通过这 4 点，所以判据是
    #      "没参与拟合的点能不能被同一套几何解释"（能解释=自洽，差很多=离群点）----
    from aihoop.court import Calibration, find_homography
    best = None
    tried = []
    for combo in itertools.combinations(range(len(pts)), 4):
        cs = [pts[i] for i in combo]
        sx = max(p["m"][0] for p in cs) - min(p["m"][0] for p in cs)
        sy = max(p["m"][1] for p in cs) - min(p["m"][1] for p in cs)
        spread = min(sx, sy)
        try:
            H = find_homography([list(p["px"]) for p in cs], [list(p["m"]) for p in cs])
        except Exception as e:                       # noqa: BLE001
            tried.append((combo, None, f"{type(e).__name__}（点退化/共线）"))
            continue
        own, off = [], []
        bad = False
        for idx, p in enumerate(pts):
            got = safe_apply_h(H, *p["px"])
            if got is None:
                bad = True
                break
            d = math.hypot(got[0] - p["m"][0], got[1] - p["m"][1])
            (own if idx in combo else off).append((p["name"], d))
        if bad:
            tried.append((combo, None, "退化（投不动）"))
            continue
        worst_off = max((d for _, d in off), default=0.0)
        tried.append((combo, worst_off, f"展布{spread:.1f}m 拟合内最大残差{max(d for _,d in own):.4f}m"))
        if spread < args.min_spread_m:
            continue
        # 判据：没参与拟合的点偏差越小，说明这套点越自洽；并列时取展布更大者
        key = (round(worst_off, 3), -spread)
        if best is None or key < best[0]:
            best = (key, combo, H, spread, off)

    print("\n候选 4 点组合（判据 = 没参与拟合的点差多少，越小越自洽）：")
    for combo, worst, note in tried:
        names = "+".join(pts[i]["name"].replace("lane_", "").replace("_baseline", "_b") for i in combo)
        mark = "  ← 选中" if best and combo == best[1] else ""
        print(f"  {names:48} {'离群偏差 %.3f m' % worst if worst is not None else note}{mark}")
    if best is None:
        print("\n没有可用组合（展布要求太严？调小 --min-spread-m）")
        return 1

    _, combo, H, spread, off = best
    chosen = [pts[i]["name"] for i in combo]
    print(f"\n选中：{chosen}（展布 {spread:.2f} m）")
    outliers = [(n, d) for n, d in off if d > 0.5]
    for n, d in off:
        flag = "  ★ 离群：与其它点不自洽" if d > 0.5 else ""
        print(f"  未参与拟合的 {n:22} 差 {d:7.3f} m{flag}")

    # ---- 自检：篮筐必须落在 (0, ±1.575)，且能投回画面内 ----
    problems = []
    for name, (ex, ey) in EXPECT_RANGES.items():
        if any(p["name"] == name for p in pts):
            p = next(p for p in pts if p["name"] == name)
            got = safe_apply_h(H, *p["px"])
            if got is None:
                problems.append(f"{name} 投不动（单应退化）")
                continue
            gx, gy = got
            d = math.hypot(gx - ex, gy - ey)
            print(f"  {name:22} 拟合后 ({gx:7.3f},{gy:7.3f})  期望 ({ex:7.3f},{ey:7.3f})  差 {d:.4f} m")
            if d > 0.15:
                problems.append(f"{name} 偏差 {d:.3f} m（>0.15）")
    for n, d in outliers:
        problems.append(f"{n} 与其它点差 {d:.3f} m（>0.5）：**这个点本身可能标错了**，"
                        f"已从标定里剔除；建议回旧项目重标它")
    # 篮筐反投影：应当落在画面内、且在画面上部（篮筐在空中，但地面点用于自检）
    gy_px = None
    try:
        from aihoop.court import apply_homography
        inv = _invert(H)
        hx, hy = apply_homography(inv, 0.0, Y_SHIFT)
        gy_px = (hx, hy)
        print(f"  篮筐地面点反投影到像素 ({hx:.0f},{hy:.0f})（用于确认标定方向没反）")
    except Exception as e:                            # noqa: BLE001
        print(f"  [warn] 篮筐反投影失败：{type(e).__name__}: {e}")

    cal = {"name": "from_hoopai_" + src.parent.name,
           "method": "manual",
           "src_px": [list(pts[i]["px"]) for i in combo],
           "dst_m": [list(pts[i]["m"]) for i in combo],
           "H": H,
           "reproj_error_m": 0.0,     # 4 点精确拟合，这 4 点残差为 0
           "note": (f"由 HoopAI {src.name} 转换：y += {Y_SHIFT}；从 {len(pts)} 个点里取自洽的 4 点 "
                    f"{[pts[i]['name'] for i in combo]} 重拟合"
                    + (f"；剔除离群点 {outliers}" if outliers else "")),
           "frame": "half",
           "for_video": args.for_video,
           "frame_size": old.get("frame_size") or []}
    out = Path(args.out) if args.out else (ROOT / "data" / "calibration_from_hoopai.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 读回验证：用 aihoop 自己的 Calibration.load + to_court ----
    c = Calibration.load(str(out))
    ok = True
    for i in combo:
        gx, gy = c.to_court(*pts[i]["px"])
        ex, ey = pts[i]["m"]
        d = math.hypot(gx - ex, gy - ey)
        if d > 0.01:
            ok = False
            print(f"  ✗ 读回后 {pts[i]['name']} 差 {d:.4f} m（应≈0）")
    print(f"\n写出：{out}")
    print(f"  用 Calibration.load 读回：frame={c.frame} 点数={len(c.src_px)} "
          f"reproj={c.reproj_error_m} matches_video={c.matches_video(args.for_video or '')}")
    print(f"  权重占比：篮筐地面点 (0,{Y_SHIFT}) ↔ 底线 |y|=0 —— 与 aihoop 约定一致")

    if problems:
        print("\n需要注意：")
        for x in problems:
            print("  ⚠ " + x)
        print("\n结论：标定文件已生成且可被 aihoop 读入，但**上面标 ⚠ 的点先回旧项目重标一次**更划算"
              "（重标 1 个点比事后猜误差来源便宜得多）。")
        return 1 if not ok else 0
    print("\n结论：自检通过。真视频端到端（用你训练好的 v6 权重）：")
    print(f"  python -m aihoop.cli video --video <视频> --cal {out} \\")
    print("      --hoop-weights D:\\dsh_folder\\hoopai\\models\\rim_ball_v6.pt \\")
    print("      --ball-weights D:\\dsh_folder\\hoopai\\models\\rim_ball_v6.pt --no-scoreboard")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
