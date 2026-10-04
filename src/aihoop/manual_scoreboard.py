"""手动框选记分牌 → 读出得分事件（不依赖模板库，任何样式都能用）。

为什么需要它：
  现有比分牌识别靠"模板匹配"（`scoreboard.py` 的 FieldPatterns），
  只能认常见样式。实测 Attleboro 这段（顶部横条 + 右侧 LED）模板匹配直接报
  「画面里没有找到广播比分牌」—— 于是既没有比分，也没有"什么时候得的"。

思路（避开"认字体"这件难事）：
  1. 用户框出**两个数字区域**（主队分 / 客队分），并给出**起始比分**
     （比如此刻 KPHS 27、AHS 35）；
  2. 逐帧把区域内的数字切成字形（连通域 + 归一化 + 哈希）；
  3. 起始帧的字形直接对应起始比分的各位数字 → 建起"字形→数字"字典；
  4. 后面遇到没见过的字形，用两个硬约束推：
       * 比分**只增不减**；
       * 一次变化最多 +3（篮球一次进攻最多 3 分，罚球 1 分）。
     满足这两个约束的解通常唯一，唯一不了的标记为"不确定"并跳过（不瞎猜）。
  5. 值变大 → 产出一条得分事件 {t, team, delta}，交给上层
     （再"向前找 1.5 秒"归到球员）。

这样"什么时候得分、哪队、几分"就都有了，且**不需要球场标定**。
"""
from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

GLYPH_W, GLYPH_H = 12, 18      # 归一化尺寸
HASH_W, HASH_H = 8, 12         # 匹配用的二值位图（96 位）
HASH_TOL = 12                  # 96 位里允许的差异位数（抗噪容差）


def _hamming(a: bytes, b: bytes) -> int:
    """两个二值位图的汉明距离（不同位的个数）。"""
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b))


def _match(bits: bytes, digit_map: dict):
    """在字典里找最像的字形（容差匹配）。返回 (数字, 距离) 或 (None, 距离)。"""
    best_d, best_v = 10 ** 9, None
    for kb, kb_digit in digit_map.items():
        d = _hamming(bits, kb)
        if d < best_d:
            best_d, best_v = d, kb_digit
    if best_v is not None and best_d <= HASH_TOL:
        return best_v, best_d
    return None, best_d


def _crop(img, region):
    x0, y0, x1, y1 = [int(round(v)) for v in region]
    h, w = img.shape[:2]
    x0 = max(0, min(w - 2, x0)); x1 = max(x0 + 2, min(w, x1))
    y0 = max(0, min(h - 2, y0)); y1 = max(y0 + 2, min(h, y1))
    return img[y0:y1, x0:x1]


def _glyphs(region_img) -> list[tuple[bytes, int]]:
    """把区域里的数字切成字形，返回 [(字形哈希, 字形宽度)]，按从左到右排序。

    处理：灰度 → 自适应二值（自动判断"深字浅底"还是"浅字深底"）→
    连通域 → 过滤太小/太大的 → 按 x 排列。
    """
    if region_img.size == 0:
        return []
    g = cv2.cvtColor(region_img, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (3, 3), 0)
    # Otsu 自动阈值；再看哪种极性更"像字"（前景占比 3%~60%）
    _t, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cands = [bw, cv2.bitwise_not(bw)]
    best = None
    for m in cands:
        frac = float((m > 0).mean())
        score = 1.0 if 0.03 <= frac <= 0.6 else 0.0
        if best is None or score > best[0]:
            best = (score, m, frac)
    m = best[1]
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(m, 8)
    h = g.shape[0]
    boxes = []
    for i in range(1, n):
        x, y, w, hh, a = (int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]),
                          int(stats[i, 3]), int(stats[i, 4]))
        if hh < max(4, int(h * 0.35)) or hh > h:      # 太矮的多半是噪点
            continue
        if w < 1 or w > hh * 1.6:                     # 数字宽高比大致 0.3~1.6
            continue
        if a < max(4, int(0.06 * hh * w)):
            continue
        boxes.append((x, y, w, hh))
    boxes.sort(key=lambda b: b[0])
    out = []
    for (x, y, w, hh) in boxes:
        sub = m[y:y + hh, x:x + w]
        sub = cv2.resize(sub, (GLYPH_W, GLYPH_H), interpolation=cv2.INTER_AREA)
        # 再压成 8x12 的**二值**位图（96 位）—— 帧间噪声不再影响匹配
        small = cv2.resize(sub, (HASH_W, HASH_H), interpolation=cv2.INTER_AREA)
        bits = (small > 127).astype(np.uint8)
        out.append((bits.tobytes(), w))
    return out


def read_region_series(video: str, region, step_s: float = 0.25,
                       max_seconds: float = 0.0):
    """逐帧读一个区域，返回 [(t, [字形哈希...])]。"""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / fps if fps else 0.0
    if max_seconds and max_seconds > 0:
        dur = min(dur, max_seconds)
    step = max(1, int(round(step_s * fps)))
    series = []
    f = 0
    while f < total:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            break
        gl = _glyphs(_crop(fr, region))
        if gl:
            series.append((round(f / fps, 2), [g[0] for g in gl]))
        f += step
    cap.release()
    return series


def _candidate_values(cur_digits: list, digit_map: dict, prev: int) -> list:
    """给未见过字形试所有数字组合，返回满足「只增不减 + 最多 +3」的候选。

    返回 [(值, {新字形: 数字})]，按值排序。
    注意：这里对**唯一字形**做组合（不是对每个位置），否则 "33" 这种重复数字会错位。
    """
    import itertools
    unknown = []
    for k in cur_digits:
        if k not in digit_map and k not in unknown:
            unknown.append(k)
    if len(unknown) > 4:            # 组合太多，说明这次读数异常，放弃
        return []
    out = []
    for combo in itertools.product(range(10), repeat=len(unknown)):
        m = dict(digit_map)
        for k, d in zip(unknown, combo):
            m[k] = d
        try:
            val = int("".join(str(m[k]) for k in cur_digits))
        except Exception:  # noqa: BLE001
            continue
        if prev <= val <= prev + 3:
            out.append((val, dict(zip(unknown, combo))))
    out.sort(key=lambda x: x[0])
    return out


def read_scoreboard_regions(video: str, regions: dict, start: dict,
                            step_s: float = 0.25,
                            max_seconds: float = 0.0) -> dict:
    """读出手动框选的记分牌，返回得分事件与诊断。

    regions: {"home": (x0,y0,x1,y1), "away": (...)}
    start:   {"home": 27, "away": 35}（第一帧看到的比分，用来给字形定值）
    """
    series = {k: read_region_series(video, r, step_s=step_s,
                                    max_seconds=max_seconds)
              for k, r in regions.items()}
    # 以两个区域**共同**出现的时刻为准（避免一边漏读导致错位）
    times = None
    for k, s in series.items():
        tset = [t for t, _ in s]
        times = tset if times is None else [t for t in times if t in tset]
    times = times or []
    idx = {k: {t: g for t, g in series[k]} for k in series}

    # 起始帧建字典：**不能只信第一帧**。
    # 实测 t=0 时画面还没稳定（淡入/黑帧），"35" 会被粘成 1 个连通域，
    # 于是判定"位数不符"直接失败。所以在开头几秒里找一个字形数正好等于
    # 起始比分位数的采样点来建字典。
    digit_map: dict[bytes, int] = {}
    cur = {}
    for team in ("home", "away"):
        v = int(start.get(team, 0))
        s = str(v)
        ref = None
        for t in times[: max(1, int(6.0 / max(0.05, step_s)))]:
            gl = idx[team].get(t)
            if gl and len(gl) == len(s):
                ref = (t, gl)
                break
        if ref is None:
            counts = [len(idx[team].get(t) or []) for t in times[:8]]
            return {"ok": False, "reason":
                    f"{'主' if team == 'home' else '客'}队起始比分 {v} 是 {len(s)} 位，"
                    f"但开头几秒区域里切出的字形数是 {counts} —— "
                    "请检查框选范围是否正好只包含数字（不要带上队名/冒号）。"}
        for k, ch in zip(ref[1], s):
            digit_map.setdefault(k, int(ch))
        cur[team] = v

    events = []
    uncertain = []
    for t in times:
        for team in ("home", "away"):
            gl = idx[team].get(t) or []
            if not gl:
                continue
            # 先按容差匹配：每位数字找最像的已知字形
            digits, unknown_pos = [], []
            for pos, k in enumerate(gl):
                v, _d = _match(k, digit_map)
                digits.append(v)
                if v is None:
                    unknown_pos.append(pos)
            if not unknown_pos:
                val = int("".join(str(d) for d in digits))
            else:
                # 有没见过的字形 → 用"只增不减 + 最多 +3"推（通常唯一）
                cands = _candidate_values(gl, digit_map, cur[team])
                if len(cands) == 1:
                    val, new_map = cands[0]
                    digit_map.update(new_map)
                else:
                    uncertain.append({"t": t, "team": team,
                                      "cands": [c[0] for c in cands[:5]],
                                      "prev": cur[team],
                                      "unknown_pos": unknown_pos})
                    continue
            if val < cur[team]:
                uncertain.append({"t": t, "team": team, "read": val,
                                  "prev": cur[team], "note": "读数回退，按误读丢弃"})
                continue
            if val > cur[team]:
                events.append({"t": t, "team": team, "delta": val - cur[team],
                               "value": val})
                cur[team] = val
    return {"ok": True, "events": events, "uncertain": uncertain,
            "final": dict(cur), "n_samples": len(times),
            "digit_map_size": len(digit_map),
            "series": {k: len(v) for k, v in series.items()}}
