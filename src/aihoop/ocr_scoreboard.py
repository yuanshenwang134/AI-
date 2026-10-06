"""用 Windows OCR 读取广播比分牌。

为什么要有这个：
  scoreboard.py 的模板匹配要求「绿色渐变横条 + 白字」这一种台标。
  实际转播里还有很多别的样式（例如 NBA 底部深色条 + 白色队名/比分），
  模板匹配会一帧都读不到。Windows 10/11 自带 OCR 引擎，可以读出
  「THUNDER 90 TIMBERWOLVES 79 3rd Qtr 2:06」这种文本。

设计：
  * 只抽少量帧（默认 12 帧），只 OCR 画面底部一条，速度快；
  * 把 OCR 的每个词连同位置一起交给解析器，用「队名后面最近的那个数字」
    来配对比分，避免 OCR 把词序读乱；
  * 解析成功后走和模板比分牌完全一样的 _debounce / score_points_to_attempts
    流程；
  * 失败返回 None，调用方自动退回模板匹配或球+篮筐路径。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

STOPWORDS = {
    "BONUS", "QTR", "QUARTER", "TARGET", "CENTER", "CENTRE", "FITBIT",
    "STATE", "FARM", "NBA", "TIMEOUT", "REPLAY", "CHALLENGE", "OVERTIME",
    "OT", "FINAL", "HALF", "END", "SHOT", "CLOCK", "TEAM", "FOUL", "FOULS",
    "TURNOVER", "TMO", "NBA", "TV", "TIMEOUTS", "PTS", "REB", "AST",
}


def _powershell() -> Optional[str]:
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _speakable(video_path: str) -> bool:
    return sys.platform.startswith("win") and os.path.exists(video_path)


def _extract_ocr_images(video_path: str, tmpdir: str, prefix: str,
                        samples: int = 12,
                        bottom_frac: float = 0.30, region=None,
                        zoom: float = 2.0):
    """抽帧存成给 OCR 用的 PNG。

    ``region`` = (x, y, w, h)：只截这块（通常就是自动定位到的比分牌）。
    给了它就不再"截画面下方 30%"—— 实测整帧 OCR 认不出 12px 高的比分数字，
    **放大 4 倍**后同一套 OCR 能稳定读出「KPHS 27 AHS 35」（这是手动框选那条路
    实测有效的做法，这里把它自动化）。
    """
    import cv2
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [], []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 0:
        cap.release()
        return [], []
    n = max(2, min(int(samples), total))
    idxs = [int(round(i * (total - 1) / (n - 1))) for i in range(n)]
    times, files = [], []
    for k, idx in enumerate(idxs):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        if region is not None:
            x0, y0, rw, rh = (int(v) for v in region)
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(w, x0 + max(8, rw)), min(h, y0 + max(8, rh))
            crop = frame[y0:y1, x0:x1]
        else:
            y0 = int(h * (1.0 - bottom_frac))
            crop = frame[y0:h, :]
        if crop.size == 0:
            continue
        crop = cv2.resize(crop, None, fx=zoom, fy=zoom,
                          interpolation=cv2.INTER_CUBIC)
        path = os.path.join(tmpdir, f"{prefix}{k:03d}.png")
        if not cv2.imwrite(path, crop):
            continue
        times.append(idx / fps)
        files.append(path)
    cap.release()
    return times, files


def _clean_token(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("，", "").replace("。", "").replace("：", ":")
    s = s.strip(" .,:;|[](){}<>/-_")
    return s


def _nearest_number_right(team, nums, max_gap: float):
    tx, ty, _tname, tw, th = team
    best = None
    for sx, sy, _sval, _sw, _sh in nums:
        if abs(sy - ty) > max(22.0, 1.8 * th):
            continue
        gap = sx - (tx + tw)
        if gap < -max_gap or gap > max_gap:
            continue
        d = abs(gap)
        if best is None or d < best[0]:
            best = (d, team, nums[nums.index((sx, sy, _sval, _sw, _sh))])
    return best


PERIOD_MARKS = {"1ST", "2ND", "3RD", "4TH", "OT", "OT1", "OT2", "Q1", "Q2",
                "Q3", "Q4", "QTR", "QUARTER", "H1", "H2", "1ST.", "2ND.",
                "3RD.", "4TH."}


def _parse_pairs(words: list[dict]) -> Optional[dict]:
    """按**版式**配对：每个比分数字配它**左边**最近的那个词。

    为什么需要这条（实测踩到）：`_parse_words` 要求队名 Token ≥4 个字母，
    这段转播的台标是 `DoubleACS | KPHS 27 | AHS 35 | 3rd Qtr`，
    `AHS` 只有 3 个字母被丢掉 → 只剩 KPHS 一个"队名" →
    两个比分都配到了同一个数字，读出 **27 : 27**（而画面是 27 : 35）。

    这条规则更贴实际版式：**队名在数字左边**（"KPHS 27"、"AHS 35"），
    所以每个数字往左找最近的词就是它的队名；再排除节次标记（3rd/4th/Q1…）
    与时钟（带冒号的 token 不是纯数字，天然排除）。
    """
    if not words:
        return None
    toks = []
    for w in words:
        try:
            toks.append({"x": float(w.get("x", 0)), "y": float(w.get("y", 0)),
                         "w": float(w.get("w", 0)), "h": float(w.get("h", 0)),
                         "text": _clean_token(str(w.get("text", "")))})
        except Exception:
            continue
    toks = [t for t in toks if t["text"]]
    if not toks:
        return None
    nums = [t for t in toks
            if t["text"].isdigit() and 0 <= int(t["text"]) <= 199]
    if len(nums) < 2:
        return None
    # 剔掉时钟（两个紧挨着的数字）与节次（`|` 右边的数字）——
    # 否则 `7:40` 会被当成客队比分（实测把 0:0 读成 0:7）。
    nums = _drop_clock_and_period(
        [(t["x"], t["y"], int(t["text"]), t["w"], t["h"]) for t in nums],
        words)
    nums = [{"x": float(x), "y": float(y), "w": float(w), "h": float(h),
             "text": str(v)} for (x, y, v, w, h) in nums]
    if len(nums) < 2:
        return None
    words_ = [t for t in toks if not t["text"].isdigit()]
    pairs = []
    for n in sorted(nums, key=lambda t: t["x"]):
        left = [t for t in words_
                if t["x"] + t["w"] <= n["x"] + 2
                and abs(t["y"] - n["y"]) <= max(22.0, 1.6 * max(t["h"], n["h"]))]
        if not left:
            continue
        l = max(left, key=lambda t: t["x"] + t["w"])
        name = l["text"].upper()
        if name in PERIOD_MARKS or name in STOPWORDS:
            continue
        pairs.append({"name": name, "val": int(n["text"]),
                      "name_x": l["x"], "num_x": n["x"], "y": n["y"]})
    # 同一行 + 去掉重复数值：真正的两个比分不会重名重值
    if len(pairs) < 2:
        return None
    pairs.sort(key=lambda p: p["num_x"])
    y0 = pairs[0]["y"]
    row = [p for p in pairs if abs(p["y"] - y0) <= 24.0] or pairs
    if len(row) < 2:
        return None
    home, away = row[0], row[1]
    period = 3
    for t in toks:
        up = t["text"].upper()
        m = re.match(r"^(?:Q|OT)?(\d)(?:ST|ND|RD|TH)?$", up)
        if m and up in PERIOD_MARKS:
            period = int(m.group(1))
            break
    return {"home": home["val"], "away": away["val"], "period": period,
            "confidence": 0.7,
            "home_name": home["name"], "away_name": away["name"]}


def _drop_clock_and_period(nums: list, words: list) -> list:
    """从候选数字里剔除**时钟**和**节次**，只留可能是比分的数字。

    为什么必须做（实测，两个时刻都验证过）：OCR 会把时钟 `7:40` 拆成两个独立
    词块 `'7'` 和 `'40'`，它们也是"数字"，于是被当成队名旁边的比分 ——
    实测 t=0 真值 `FAIRFIELD 0 SYCAMORE 0 7:40` 被读成 **`0:7`**。

    判据（篮球比分牌的铁律，不依赖具体样式）：
      * 时钟永远是**两个紧挨着的数字**（中间那个冒号 OCR 读不出来）——
        实测 `'5'`(x=2560,w=40) 与 `'46'`(x=2623) 之间只隔 23px，而字高 72px。
        所以"间隙 < 0.8×字高"的相邻数字对判为时钟，一起剔除；
      * `|` 分隔符**右边**的数字是节次（`1st` 的 1），剔除。
    """
    if not nums:
        return nums
    # ① `|` 分隔符的 x（取最右一个：节次在它右边）
    #    ⚠️ 必须用**原始**文本比对：`_clean_token` 会把 `|` 剥掉
    #    （它 strip 了 ".,:;|[](){}<>/-_"），用清洗后的文本永远匹配不到。
    sep_x = None
    for w in (words or []):
        if str(w.get("text", "")).strip() == "|":
            try:
                sx = float(w.get("x", 0))
            except Exception:                                  # noqa: BLE001
                continue
            sep_x = sx if sep_x is None else max(sep_x, sx)
    kept = [n for n in nums if not (sep_x is not None and float(n[0]) > sep_x)]

    # ② 时钟：按 x 排序后，间隙远小于字高的相邻数字对
    kept.sort(key=lambda n: float(n[0]))
    drop = set()
    for i in range(len(kept) - 1):
        ax, _ay, _av, aw, ah = kept[i]
        bx = float(kept[i + 1][0])
        gap = bx - (float(ax) + float(aw))
        h = max(float(ah), float(kept[i + 1][4]), 1.0)
        if gap < 0.8 * h:
            drop.add(i)
            drop.add(i + 1)
    return [n for j, n in enumerate(kept) if j not in drop]


def _parse_words(words: list[dict]) -> Optional[dict]:
    if not words:
        return None
    teams = []
    nums = []
    qtr_tokens = []
    for w in words:
        try:
            x = float(w.get("x", 0))
            y = float(w.get("y", 0))
            ww = float(w.get("w", 0))
            hh = float(w.get("h", 0))
        except Exception:
            continue
        text = _clean_token(str(w.get("text", "")))
        if not text:
            continue
        up = text.upper()
        if up in ("QTR", "QUARTER", "QTR.", "QTRS"):
            qtr_tokens.append((x, y, ww, hh))
        if up.isdigit():
            val = int(up)
            if 0 <= val <= 199:
                nums.append((x, y, val, ww, hh))
        elif up.isalpha() and len(up) >= 4 and up not in STOPWORDS:
            teams.append((x, y, up, ww, hh))
    if len(teams) < 2 or len(nums) < 2:
        return None
    # 同样要剔掉时钟与节次（`_parse_words` 这条路也会把 7:40 的 7 当比分）。
    # 传**原始**词块（不经过 _clean_token，否则 `|` 会被剥掉）。
    nums = _drop_clock_and_period(
        nums, [{"x": w.get("x"), "y": w.get("y"), "w": w.get("w"),
                "h": w.get("h"), "text": str(w.get("text", ""))}
               for w in words])
    if len(nums) < 2:
        return None
    widths = [t[3] for t in teams]
    max_gap = max(180.0, 3.2 * sorted(widths)[len(widths) // 2])
    pairs = []
    for team in teams:
        best = None
        for num in nums:
            sx, sy, sval, sw, sh = num
            tx, ty, tname, tw, th = team
            if abs(sy - ty) > max(22.0, 1.8 * th):
                continue
            gap = sx - (tx + tw)
            if gap < -max_gap or gap > max_gap:
                continue
            d = abs(gap)
            if best is None or d < best[0]:
                best = (d, tname, sval, tx)
        if best is not None:
            pairs.append(best)
    if len(pairs) < 2:
        return None
    # 去掉重复队伍/比分，按队名 x 排序：左边 = home
    uniq = {}
    for _d, name, val, x in pairs:
        if name not in uniq:
            uniq[name] = (x, val)
    if len(uniq) < 2:
        return None
    ordered = sorted(uniq.items(), key=lambda kv: kv[1][0])[:2]
    home_name, (hx, home) = ordered[0]
    away_name, (ax, away) = ordered[1]

    # period + clock
    period = 1
    if qtr_tokens:
        qx, qy, qw, qh = qtr_tokens[0]
        cand = [(abs(nx - qx), int(nv)) for nx, ny, nv, nw, nh in nums
                if abs(ny - qy) < 40 and 1 <= int(nv) <= 4]
        if cand:
            period = min(cand)[1]

    clock = None
    joined = " ".join(str(w.get("text", "")) for w in words)
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", joined)
    if m:
        clock = int(m.group(1)) * 60 + int(m.group(2))
    return {"home": int(home), "away": int(away),
            "home_name": home_name, "away_name": away_name,
            "period": int(period), "clock": clock, "confidence": 0.85}


def _parse_text_only(text: str) -> Optional[dict]:
    """没有词框时的退路：从文本里抓第一组「队名 数字」。"""
    up = text.upper()
    pairs = re.findall(r"([A-Z][A-Z0-9 .'\-]{3,}?)\s+(\d{1,3})", up)
    cleaned = []
    for name, val in pairs:
        nm = name.strip()
        if nm in STOPWORDS or len(nm) < 4:
            continue
        try:
            iv = int(val)
        except Exception:
            continue
        if 0 <= iv <= 199:
            cleaned.append((nm, iv))
    # 去重并保留前两个不同队名
    seen = {}
    for nm, val in cleaned:
        if nm not in seen:
            seen[nm] = val
        if len(seen) >= 2:
            break
    if len(seen) < 2:
        return None
    items = list(seen.items())
    return {"home": items[0][1], "away": items[1][1],
            "home_name": items[0][0], "away_name": items[1][0],
            "period": 1, "clock": None, "confidence": 0.6}


def _auto_candidates(w: int, h: int) -> list:
    """自动定位的候选区域（归一化 x,y,w,h）—— **按常见台标位置排优先级**。

    为什么要多个候选（用户实测要求："比分牌子你不能自动识别吗"）：
      原来只试"画面下方 30%"一整条。用户的素材里比分牌只在**左下角一小块**，
      放在整条宽横幅里 OCR 读不出（实测：默认配置读不出，读出来也是噪声）。
      OCR 本身很快（实测 0.9~1.3s 一次），所以多试几块是划算的。
    顺序 = 先试最常见的（下排左/中/右），再试上排，最后整条兜底。
    """
    return [
        ("下方左", 0.00, 0.72, 0.40, 0.28),
        ("下方中", 0.28, 0.72, 0.44, 0.28),
        ("下方右", 0.60, 0.72, 0.40, 0.28),
        ("上方中", 0.25, 0.00, 0.50, 0.20),
        ("上方左", 0.00, 0.00, 0.40, 0.20),
        ("上方右", 0.60, 0.00, 0.40, 0.20),
        ("中上", 0.20, 0.00, 0.60, 0.28),
        ("下方整条", 0.00, 0.70, 1.00, 0.30),
    ]


def _reading_quality(readings: list, n_frames: int, team_names: dict) -> float:
    """给一组读数打分 —— 用来在候选区域之间挑，**并且用来拒绝噪声**。

    为什么必须有（实测）：默认配置在真实素材上读出了 `0:7 / 5:2 / 5:5` ——
    **有数字但全是噪声**（客队从 7 掉到 2，篮球比分只增不减），旧逻辑会照单全收，
    然后报告里出现一个错的比分。**报错误比分比读不出来更糟**。

    真实的比分牌应当满足：
      * **帧命中率高**：同一块地方在大多数采样帧都能读出数字；
      * **单调不减**：比分只增不减；大幅回退说明读的是记分牌上的别的东西
        （犯规数、时间、球员号）；
      * **两队队名互不相同**：读成同一个名字（如两边都 `YCG_YORE`）必是噪声。
    返回 (质量分, 是否可接受, 说明)。
    """
    if not readings or n_frames <= 0:
        return 0.0, False, "没读出任何读数"
    hit = len(readings) / float(n_frames)
    pairs = list(zip(readings, readings[1:]))
    bad = 0
    for a, b in pairs:
        if int(b.home) < int(a.home) or int(b.away) < int(a.away):
            bad += 1
    mono = 1.0 - (bad / float(len(pairs))) if pairs else 1.0
    hn = str((team_names or {}).get("home") or "").strip()
    an = str((team_names or {}).get("away") or "").strip()
    names_ok = 1.0 if (hn and an and hn != an) else 0.0
    # 真实比赛中比分**很少变化** ⇒ 不同读数的占比很低；噪声则是每帧都不一样
    # （实测噪声 `0:7 / 2:6 / 5:5` → 3/3 = 1.0）。
    pairs_seen = {(int(r.home), int(r.away)) for r in readings}
    distinct_ratio = len(pairs_seen) / float(len(readings))
    q = round(hit * 3.0 + mono * 2.0 + names_ok
              + (1.0 if distinct_ratio <= 0.6 else 0.0), 3)

    why = []
    if hit < 0.40 and len(readings) < 4:
        why.append("只在 %d/%d 帧读到数字" % (len(readings), n_frames))
    if mono < 0.80:
        why.append("比分出现回退（%d/%d 次），不像比分牌"
                   % (bad, max(1, len(pairs))))
    if len(readings) > 2 and distinct_ratio > 0.6:
        why.append("每帧读数都不一样（%d 种/%d 条），不像比分牌"
                   % (len(pairs_seen), len(readings)))
    if hn and an and hn == an:
        # ⚠️ 只降分，**不否决**。实测：低清素材上队名经常读坏（两边都读成
        # FAIRFIELD），但比分 `5:5 ×3` 完全正确且一致 ——
        # 用户要的是比分，不能因为队名读重了就把正确比分丢掉（我犯过这个错）。
        why.append("队名读重了（%s）—— 比分仍可用，但不写队名" % hn)
    # **否决只看比分本身**：帧命中率、单调不减、读数一致性。
    ok = ((hit >= 0.40 or len(readings) >= 4) and mono >= 0.80
          and (len(readings) <= 2 or distinct_ratio <= 0.6))
    return q, ok, ("；".join(why) if why else "可信")


def read_scoreboard_ocr_best(video_path: str, cfg=None,
                             samples: int = 12, zoom: float = 4.0,
                             probe_samples: int = 4,
                             min_quality: float = 2.2):
    """**自动**找出比分牌并读它 —— 多候选区域扫描 + 读数质量打分。

    返回 (scan, info)：scan 为 None 表示没找到（**宁可返回 None 也不报噪声**）；
    info 里带每个候选的得分与拒绝原因，失败时能如实告诉用户试过哪些位置。

    实测成本：一次 OCR 约 1 秒，8 个候选 × 4 帧 ≈ 8~12 秒，值得。
    """
    import cv2
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None, {"tried": [], "note": "打不开视频"}
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    if W <= 0 or H <= 0:
        return None, {"tried": [], "note": "读不到画面尺寸"}

    tried = []
    best = None
    for name, nx, ny, nw, nh in _auto_candidates(W, H):
        region = (int(nx * W), int(ny * H), int(nw * W), int(nh * H))
        try:
            scan = read_scoreboard_ocr(video_path, cfg=cfg,
                                       samples=probe_samples, region=region,
                                       zoom=zoom)
        except Exception as e:                                # noqa: BLE001
            tried.append({"cand": name, "region": list(region), "q": 0.0,
                          "ok": False,
                          "note": "%s: %s" % (type(e).__name__, str(e)[:60])})
            continue
        if scan is None:
            tried.append({"cand": name, "region": list(region), "q": 0.0,
                          "ok": False, "n": 0, "note": "这块没读出数字"})
            continue
        rd = list(getattr(scan, "readings", []) or [])
        q, ok, why = _reading_quality(rd, probe_samples,
                                      getattr(scan, "team_names", {}) or {})
        tried.append({"cand": name, "region": list(region), "q": q, "ok": ok,
                      "n": len(rd), "note": why,
                      "sample": [[int(r.home), int(r.away)] for r in rd[:4]]})
        if ok and (best is None or q > best[0]):
            best = (q, name, region, scan)

    if best is None or best[0] < min_quality:
        return None, {
            "tried": tried,
            "note": ("自动定位没找到可信的比分牌（试了 %d 个常见位置，"
                     "全部被可信度校验拒掉）。各位置：%s"
                     % (len(tried),
                        "、".join("%s(%.1f:%s)" % (t["cand"], t["q"],
                                                  str(t.get("note"))[:24])
                                  for t in tried))),
            "best_q": (best[0] if best else 0.0),
        }

    q, name, region, scan = best
    # 命中候选后用**完整采样数**重跑一遍，拿到足够的事件
    if samples > probe_samples:
        try:
            full = read_scoreboard_ocr(video_path, cfg=cfg, samples=samples,
                                       region=region, zoom=zoom)
            if full is not None and getattr(full, "readings", None):
                frd = list(full.readings)
                q2, ok2, _why2 = _reading_quality(
                    frd, samples, getattr(full, "team_names", {}) or {})
                if ok2:
                    scan = full
                    q = q2
        except Exception:                                     # noqa: BLE001
            pass
    return scan, {"tried": tried, "picked": name, "region": list(region),
                  "quality": q,
                  "note": "自动定位到比分牌：%s（可信度 %.1f）" % (name, q)}


def read_scoreboard_ocr(video_path: str, cfg=None,
                        samples: int = 12, region=None,
                        zoom: float = 2.0) -> Optional["ScoreboardScan"]:
    """读比分牌；成功返回 ScoreboardScan，失败返回 None。

    ``region`` = (x, y, w, h)：比分牌在画面里的位置。给了它就**只 OCR 这块并放大**
    （实测整帧 OCR 读不出 12px 高的比分数字，放大 4 倍后能稳定读出）。
    没给就按老办法截画面下方 30%。
    """
    if not _speakable(video_path):
        return None
    ps = _powershell()
    if not ps:
        return None
    import uuid
    tmp = tempfile.gettempdir()
    prefix = f"aihoop_ocr_{uuid.uuid4().hex[:8]}_"
    files = []
    try:
        times, files = _extract_ocr_images(video_path, tmp, prefix,
                                           samples=samples, region=region,
                                           zoom=zoom)
        if not times:
            return None
        out_json = os.path.join(tmp, prefix + "ocr.json")
        script = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "scripts",
            "ocr_images.ps1")
        if not os.path.exists(script):
            return None
        cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               script, "-ImageDir", tmp, "-Pattern", prefix + "*.png",
               "-OutJson", out_json]
        subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if not os.path.exists(out_json):
            return None
        items = json.load(open(out_json, encoding="utf-8"))
        if not isinstance(items, list):
            return None
        # 按文件名排序，对齐我们保存的采样时间
        items = sorted(items, key=lambda d: str(d.get("file", "")))
        from .scoreboard import ScoreboardScan, ScoreReading, ScoreBug, _debounce
        readings = []
        team_names = {}
        for item, t in zip(items, times):
            parsed = None
            words = item.get("words") or []
            if words:
                parsed = _parse_words(words)
                # 版式兜底：`_parse_words` 会漏掉 3 字母队名（AHS），
                # 于是两边配到同一个数字（实测读出 27 : 27，真值 27 : 35）。
                # 出现"两边同分"或"两边队名相同"时，改用按版式配对的解析。
                if parsed is None or (parsed.get("home") == parsed.get("away")):
                    alt = _parse_pairs(words)
                    if alt is not None:
                        parsed = alt
            if not parsed:
                parsed = _parse_text_only(str(item.get("text", "")))
            if not parsed:
                continue
            if not team_names:
                team_names = {"home": parsed.get("home_name", ""),
                              "away": parsed.get("away_name", "")}
            readings.append(ScoreReading(
                t=round(float(t), 3),
                home=int(parsed["home"]), away=int(parsed["away"]),
                period=int(parsed.get("period") or 1),
                confidence=float(parsed.get("confidence", 0.7))))
        if not readings:
            return None
        import dataclasses
        cfg_sb = cfg
        from .scoreboard import ScoreBugConfig
        cfg_sb = cfg_sb or ScoreBugConfig()
        events = _debounce(readings, cfg_sb)
        bug = ScoreBug(x=0, y=0, w=0, h=0)
        scan = ScoreboardScan(bug=bug, readings=readings, events=events,
                              fps=30.0, duration=(times[-1] if times else 0.0),
                              frames_read=len(items), frames_hit=len(readings),
                              team_names=team_names)
        return scan
    except Exception:
        return None
    finally:
        for f in files:
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            if os.path.exists(out_json):
                os.remove(out_json)
        except Exception:
            pass
