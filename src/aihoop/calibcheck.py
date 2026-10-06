"""标定校验 + 半自动标定 —— 让「真视频能不能用」变成可测量的结论。

为什么需要这个模块
------------------------------------------------------------------
单应矩阵（拍一张、标一次、全场复用）**只对固定机位成立**。一旦镜头在推拉
摇移或切镜，同一个 H 就会把球员投到错误的位置；而错误的位置**看起来依然
很像一张战术图**，不会报错。这是整个套餐 B 里最危险的一类静默失败。

所以本模块只做三件事，全部是客观可复现的测量，不依赖肉眼：

  1. camera_motion()   机位稳不稳？ —— 相位相关测帧间位移
  2. court_fit_score() 标定准不准？ —— 把球场线投回画面，量"这地方是不是真有一条白线"
  3. render_overlay()  给人看的证据 —— 把球场线画到视频帧上存成图片

配套的交互式标定（点 4 个角点）也在这里，见 interactive_calibrate()。

判定口径（写死在这些阈值里，答辩时可以直接讲）：
  * 位移中位数 < 1.5 px/帧        -> 固定机位，单应可用
  * 位移中位数 > 4.0 px/帧        -> 镜头在动，单应**不可用**（需要逐帧关键点）
  * 线拟合分数比随机点高 1.5 倍以上 -> 标定与画面吻合
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional, Sequence

from .court import Calibration, HALF_COURT_CORNERS, FULL_COURT_CORNERS
from .model import COURT_LENGTH, COURT_WIDTH

# 判定阈值
#
# ⚠️ 位移的单位是 **px/秒**（不是 px/帧）。这个单位很容易搞错 —— 采样是每秒一帧，
# 如果沿用"每帧"的经验值（1~2px），会把固定机位的正常抖动（每秒几像素）
# 误判成"镜头在动"，进而拒绝掉本来可用的素材。实测：
#   固定广播机位 ≈ 2~5 px/s（含球员运动带来的噪声，相机本身没动）
#   手持手机     ≈ 10~40 px/s（几秒钟就能漂出画面）
MOTION_STATIC = 3.0      # px/s，低于此认为固定机位
MOTION_MOVING = 15.0     # px/s，高于此认为镜头在明显移动、单应不可用
#
# 线拟合分数的门槛按**真实素材**定。真实画面里球场线是细的、还被视频压缩糊过，
# 亮脊远不如合成图（合成图能到 90 倍）。实测一份正确的标定在真实广播画面上
# 是 1.3~2.3 倍，错误标定在 1.0 倍附近 —— 所以门槛放在 1.6 / 1.25。
FIT_RATIO_OK = 1.6
FIT_RATIO_BAD = 1.25


def _require_cv():
    try:
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "标定校验需要 opencv-python 与 numpy。安装："
            "pip install -r requirements-full.txt") from e


# --------------------------------------------------------------------------
# 1) 机位运动检测
# --------------------------------------------------------------------------
def camera_motion(video_path: str, sample_fps: float = 1.0,
                  max_frames: int = 120) -> dict:
    """用相位相关测「每秒帧间位移」，判断这个机位能不能用单应标定。

    相位相关（cv2.phaseCorrelate）对整体平移很敏感，而且**不需要特征点**，
    在低纹理的球场画面上比 ORB/光流稳。返回位移的中位数与最大值：
    中位数反映"整体是不是在慢慢摇"，最大值用来抓"切镜"。

    返回：
      {median_px, max_px, samples, verdict: "static"|"moving"|"unknown", note}
    """
    _require_cv()
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"median_px": 0.0, "max_px": 0.0, "samples": 0,
                "verdict": "unknown", "note": f"打不开视频：{video_path}"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps / max(0.1, sample_fps))))
    win = None
    prev = None
    shifts: list[float] = []
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0 and len(shifts) < max_frames:
            ok, frame = cap.retrieve()
            if not ok:
                break
            small = cv2.resize(frame, (640, 360))
            g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if win is None:
                win = cv2.createHanningWindow((g.shape[1], g.shape[0]), cv2.CV_32F)
            if prev is not None:
                (dx, dy), _resp = cv2.phaseCorrelate(prev, g, win)
                shifts.append(math.hypot(dx, dy))   # 相邻两个采样帧之间的位移
            prev = g
        idx += 1
    cap.release()
    if not shifts:
        return {"median_px": 0.0, "max_px": 0.0, "samples": 0,
                "verdict": "unknown", "note": "采样帧数不足，无法判断机位运动"}
    shifts.sort()
    # 采样间隔 = fps/step 帧；把"两个采样点之间的位移"换算成 px/秒
    secs = max(1e-6, step / max(1.0, fps))
    med = shifts[len(shifts) // 2] / secs
    mx = shifts[-1] / secs
    if med <= MOTION_STATIC:
        verdict, note = "static", "固定机位（或只有轻微抖动），单应标定可用"
    elif med <= MOTION_MOVING:
        verdict, note = "slightly_moving", (
            "镜头有可见移动：单应标定只在短时间内近似成立，"
            "战术图会有整体漂移，建议用固定机位的素材")
    else:
        verdict, note = "moving", (
            "镜头在明显移动（摇摄/手持）：**一次标定的单应矩阵不可用**，"
            "球员位置会整体错位。这一段需要逐帧球场关键点检测 + 逐帧单应"
            "（即方案里列的 KaliCalib / DeepSportRadar 那条路），"
            "不是调参能解决的。")
    return {"median_px": round(med, 2), "max_px": round(mx, 2),
            "unit": "px/s", "samples": len(shifts), "verdict": verdict,
            "note": note, "fps": fps, "frames": total}


# --------------------------------------------------------------------------
# 2) 标定与画面的吻合度
# --------------------------------------------------------------------------
# 半场（分析坐标）里要检查的球场线。y=0 是底线、|y|=14 是中圈。
def _court_polylines(half: float = 1.0) -> dict:
    W = COURT_WIDTH / 2
    out = {
        "bottom": [(-W, 0.0), (W, 0.0)],
        "sideline_left": [(-W, 0.0), (-W, -COURT_LENGTH / 2)],
        "sideline_right": [(W, 0.0), (W, -COURT_LENGTH / 2)],
    }
    if half < 1.0:
        out["halfcourt_line"] = [(-W, -COURT_LENGTH / 2), (W, -COURT_LENGTH / 2)]
    # 罚球区（5.8m x 4.9m）
    out["paint"] = [(-2.45, 0.0), (-2.45, -5.8), (2.45, -5.8), (2.45, 0.0)]
    return out


def _sample_polyline(pts: Sequence, n: int = 60) -> list:
    out = []
    for i in range(n):
        u = i / max(1, n - 1) * (len(pts) - 1)
        k = min(int(u), len(pts) - 2)
        f = u - k
        out.append((pts[k][0] + (pts[k + 1][0] - pts[k][0]) * f,
                    pts[k][1] + (pts[k + 1][1] - pts[k][1]) * f))
    return out


def _ridge_strength(gray, x: int, y: int, rad: int = 5) -> float:
    """某点附近"有没有一条比周围亮的细线"的强度（亮线 = 球场线）。

    取一个小窗口，用 (最大值 - 中位数)：地板上的普通纹理不会有明显亮脊，
    白线会有。比"整幅图阈值化"稳得多 —— 后者在观众席/球衣上会误检一大片。
    """
    import numpy as np
    h, w = gray.shape
    x0, x1 = max(0, x - rad), min(w, x + rad + 1)
    y0, y1 = max(0, y - rad), min(h, y + rad + 1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return 0.0
    patch = gray[y0:y1, x0:x1].astype(np.float32)
    return float(patch.max() - np.median(patch))


def frame_fit_detail(bgr, cal: Calibration, n_samples: int = 40) -> dict:
    """单帧的线拟合明细：每条投影线附近的亮脊 + 场内地面亮脊。

    ⚠️ 基准点必须取**投影球场内部**（那是地板），不能全画面随机取：
    广播画面的上半部分是观众席，对比度极高，全画面随机取会把基准抬到天上，
    结果一份完全正确的标定也会被判成"不吻合"。这个坑实测踩过一次。
    """
    import cv2
    import numpy as np

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    W = COURT_WIDTH / 2
    detail = {}
    on = []
    for name, pts in _court_polylines(half=0.5).items():
        vals = []
        for (mx, my) in _sample_polyline(pts, n_samples):
            px, py = cal.to_pixel(mx, my)
            xi, yi = int(round(px)), int(round(py))
            if 6 <= xi < w - 6 and 6 <= yi < h - 6:
                vals.append(_ridge_strength(gray, xi, yi))
        if vals:
            detail[name] = round(float(np.median(vals)), 1)
            on.extend(vals)
    poly = np.array([[int(a), int(b)] for a, b in [
        cal.to_pixel(-W, 0.0), cal.to_pixel(W, 0.0),
        cal.to_pixel(W, -COURT_LENGTH / 2), cal.to_pixel(-W, -COURT_LENGTH / 2)]],
        np.int32)
    x0, x1 = max(0, poly[:, 0].min()), min(w - 1, poly[:, 0].max())
    y0, y1 = max(0, poly[:, 1].min()), min(h - 1, poly[:, 1].max())
    rng = np.random.default_rng(3)
    rnd = []
    guard = 0
    while len(rnd) < 400 and guard < 20000 and x1 > x0 + 4 and y1 > y0 + 4:
        guard += 1
        xi = int(rng.integers(x0, x1))
        yi = int(rng.integers(y0, y1))
        if cv2.pointPolygonTest(poly, (float(xi), float(yi)), False) >= 0:
            rnd.append(_ridge_strength(gray, xi, yi))
    a = float(np.median(on)) if on else 0.0
    b = float(np.median(rnd)) if rnd else 0.0
    return {"on_line": round(a, 1), "floor": round(b, 1),
            "ratio": round(a / max(1.0, b), 2), "lines": detail,
            "n_on_line": len(on), "n_floor": len(rnd)}


def refine_keypoints(frames: list, landmark_map: Optional[dict] = None,
                     max_shift_px: float = 12.0,
                     passes: int = 2, step_px: float = 4.0):
    """把用户标的特征点**吸附到画面里的球场线**上（微调，不重标）。

    为什么需要（用户实测的核心卡点）：
      手工标点很难精确到几个像素，而分析端的准入门槛是"投影线与画面白线的
      吻合度 ratio ≥ 1.25"。实测用户那 6 个点几何上是对的（罚球区 paint=59），
      但底线/边线差一点（bottom=3、sideline 38/52），总分只有 1.02 —— 于是
      热区、战术图、球权全部拿不到。用户的原话是"改啊"，也就是**要工具自己解决**，
      而不是让他反复重标。

    做法：以「投影线落在亮脊上的比例」为目标（frame_fit_detail 的 ratio），
    对每个点在其邻域内做小范围网格搜索 + 逐点贪心，取让目标最大的偏移。
    只用**用户已经给的帧**，不引入新数据；位移上限 max_shift_px（默认 12px），
    避免把点拽到别的线上去。

    frames: [{"bgr": ndarray, "landmarks": {名字: [x_norm, y_norm]}}, ...]
    返回 {"landmarks": 精修后的（归一化）, "before": 原 ratio, "after": 新 ratio,
          "moved_px": {名字: 像素位移}, "passes": 实际轮数, "note": 说明}
    """
    import numpy as np
    from .court import Calibration, find_homography
    # 点名的球场坐标表由调用方传入（api.py 里那张 COURT_LANDMARKS）。
    # 不做成内部 import：api.py 会 import calibcheck，反向 import 就成环了。
    landmark_table = landmark_map or {}
    if not landmark_table:
        return {"landmarks": {}, "before": 0.0, "after": 0.0, "moved_px": {},
                "passes": 0, "note": "未给点名坐标表，未做精修"}

    if not frames:
        return {"landmarks": {}, "before": 0.0, "after": 0.0, "moved_px": {},
                "passes": 0, "note": "没有可用画面，未做精修"}

    # 统一点名集合（每帧标了哪些就用哪些）
    names = []
    for f in frames:
        for k in f.get("landmarks") or {}:
            if k not in names:
                names.append(k)
    if not names:
        return {"landmarks": {}, "before": 0.0, "after": 0.0, "moved_px": {},
                "passes": 0, "note": "没有特征点，未做精修"}

    from .api import COURT_LANDMARKS  # 延后导入避免环
    orig = {}
    for k in names:
        for f in frames:
            lm = (f.get("landmarks") or {}).get(k)
            if lm:
                orig[k] = [float(lm[0]), float(lm[1])]
                break
    cur = {k: list(v) for k, v in orig.items()}

    def score(pts: dict) -> float:
        """所有帧的吻合度中位数（对单帧异常更稳）。"""
        vals = []
        for f in frames:
            bgr = f.get("bgr")
            if bgr is None:
                continue
            h, w = bgr.shape[:2]
            lm = f.get("landmarks") or {}
            src, dst = [], []
            for k in names:
                if k not in lm:
                    continue
                p = pts.get(k)
                if not p:
                    continue
                src.append([p[0] * w, p[1] * h])
                dst.append(list(landmark_table[k]))
            if len(src) < 4:
                continue
            try:
                Hm = find_homography(src, dst)
                # ⚠️ 这里**不能**把 Hm 转成 np.ndarray：Calibration.to_pixel 里写的是
                # `inv = invert(self.H) if self.H else None`，而 numpy 数组的真值判断
                # 会抛 "truth value of an array is ambiguous" —— 精修器会因此静默
                # 一步不动（实测：位移全是 0）。传 list 就没这个问题。
                cal = Calibration(name="snap", method="snap", src_px=src,
                                  dst_m=dst, H=list(Hm), frame="full",
                                  frame_size=[w, h])
                d = frame_fit_detail(bgr, cal)
            except Exception:                       # noqa: BLE001
                continue
            r = d.get("ratio")
            if isinstance(r, (int, float)) and r > 0:
                vals.append(float(r))
        if not vals:
            return 0.0
        return float(np.median(vals))

    before = score(cur)
    best = dict(cur)
    best_score = before
    used_passes = 0
    half = max(1.0, float(max_shift_px))
    steps = [s for s in (step_px, step_px / 2.0, step_px / 4.0) if s > 0.4]
    for p_i in range(max(1, int(passes))):
        used_passes = p_i + 1
        improved = False
        for k in names:
            base = best.get(k)
            if not base:
                continue
            # 以当前最优为中心做小网格
            cands = []
            for dx in (-half, -half / 2, 0.0, half / 2, half):
                for dy in (-half, -half / 2, 0.0, half / 2):
                    cands.append((dx, dy))
            local_best = list(base)
            local_score = best_score
            for (dx, dy) in cands:
                if dx == 0.0 and dy == 0.0:
                    continue
                trial = {kk: list(vv) for kk, vv in best.items()}
                # 位移以**原始点**为基准限幅，防止越走越远
                o = orig.get(k, base)
                trial[k] = [o[0] + dx / 1000.0, o[1] + dy / 1000.0]
                s = score(trial)
                if s > local_score + 1e-6:
                    local_score = s
                    local_best = trial[k]
            if local_best != base:
                best[k] = local_best
                best_score = local_score
                improved = True
        half = max(1.0, half / 2.0)          # 每轮收缩搜索半径
        if not improved:
            break

    moved = {}
    for k in names:
        o = orig.get(k)
        if not o:
            continue
        for f in frames:
            bgr = f.get("bgr")
            if bgr is None:
                continue
            h, w = bgr.shape[:2]
            dx = (best[k][0] - o[0]) * w
            dy = (best[k][1] - o[1]) * h
            moved[k] = round(float((dx * dx + dy * dy) ** 0.5), 1)
            break

    # ---- 护栏：精修结果必须**几何上仍然像球场**，否则回退到原始点 ----
    #
    # 为什么必须有（实测踩到）：这一机位几乎是正对球场，场地在画面里是个很扁的
    # 梯形 —— 这种视角下单应矩阵**天然病态**，纵深方向几乎没有信息。
    # 于是"把点挪到最亮处"这个目标会被退化解满足：实测吻合度从 1.20 冲到 74.0
    # （正常标定在真实画面上只有 1.3~2.3），点只移了 4~12 像素，但球场整体已经歪了。
    # 用一个坏目标自动改用户的点，等于在骗分数 —— 所以这里加验收，不合格就**不动**。
    why = ""
    if _snapped_geometry_sane(best, frames, landmark_table, orig):
        final, final_score = best, best_score
    else:
        final, final_score = dict(orig), before
        why = "；精修结果几何上不像球场（这机位太正对、单应矩阵病态），已保留你原来标的点"
    moved = {k: (0.0 if not why else moved.get(k, 0.0)) for k in moved}
    note = ("精修：吻合度 %.2f → %.2f（点最多移动 %.1f 像素）%s"
            % (before, final_score, max(moved.values()) if moved else 0.0, why))
    return {"landmarks": {k: [round(v[0], 5), round(v[1], 5)]
                          for k, v in final.items()},
            "before": round(before, 3), "after": round(final_score, 3),
            "moved_px": moved, "passes": used_passes,
            "accepted": not why, "note": note}


def _snapped_geometry_sane(pts: dict, frames: list, landmark_table: dict,
                           orig: dict) -> bool:
    """精修后的点解出的球场，几何上还说得通吗？

    三条硬约束（都来自"这必须是个球场"这个事实）：
      ① 吻合度不能是退化解那种离谱值（实测退化解 74.0；正常 1.3~2.3；
         放宽到 12 已经很宽容）；
      ② 投影出来的球场在画面里要有合理大小（不能缩成一条线）；
      ③ 每个点相对原始位置的位移不能超过 20 像素。
    """
    import numpy as np
    from .court import Calibration, find_homography
    for f in frames:
        bgr = f.get("bgr")
        if bgr is None:
            continue
        h, w = bgr.shape[:2]
        lm = f.get("landmarks") or {}
        src, dst = [], []
        for k in pts:
            if k not in lm or k not in landmark_table:
                continue
            src.append([pts[k][0] * w, pts[k][1] * h])
            dst.append(list(landmark_table[k]))
        if len(src) < 4:
            return False
        try:
            Hm = find_homography(src, dst)
            cal = Calibration(name="chk", method="chk", src_px=src, dst_m=dst,
                              H=list(Hm), frame="full", frame_size=[w, h])
            d = frame_fit_detail(bgr, cal)
            r = d.get("ratio")
        except Exception:                                # noqa: BLE001
            return False
        # ① 退化解比值离谱
        if not isinstance(r, (int, float)) or r > 12.0:
            return False
        # ② 投影出的球场要有合理大小（用四角在画面里的包围盒衡量）
        try:
            c = [cal.to_pixel(*p) for p in ((-7.5, 0.0), (7.5, 0.0),
                                            (7.5, -14.0), (-7.5, -14.0))]
            xs = [float(p[0]) for p in c]
            ys = [float(p[1]) for p in c]
            if not np.all(np.isfinite(xs + ys)):
                return False
            if (max(xs) - min(xs)) < w * 0.15 or (max(ys) - min(ys)) < h * 0.08:
                return False
        except Exception:                                # noqa: BLE001
            return False
    # ③ 位移上限
    for k, v in pts.items():
        o = orig.get(k)
        if not o:
            continue
        for f in frames:
            bgr = f.get("bgr")
            if bgr is None:
                continue
            h, w = bgr.shape[:2]
            dx = (v[0] - o[0]) * w
            dy = (v[1] - o[1]) * h
            if (dx * dx + dy * dy) ** 0.5 > 20.0:
                return False
            break
    return True


def court_fit_score(video_path: str, cal: Calibration,
                    times: Optional[Sequence[float]] = None,
                    n_samples: int = 50) -> dict:
    """把球场线投影回画面，量"这些地方是不是真的有一条白线"。

    做法：沿每条投影线取 n 个采样点，算 ridge 强度；再在同一帧里随机取
    若干点算 ridge 强度作为基准。**对齐时投影线上的亮脊应显著高于随机点**。

    返回每帧的 ratio（投影线 ridge / 随机点 ridge）与总分。
    """
    _require_cv()
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"ok": False, "ratio": 0.0, "note": f"打不开视频：{video_path}"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / fps if fps else 0.0
    if not times:
        times = [dur * f for f in (0.05, 0.3, 0.55, 0.8) if dur > 0] or [0.0]

    per_frame = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, frame = cap.read()
        if not ok:
            continue
        d = frame_fit_detail(frame, cal, n_samples)
        d["t"] = round(t, 2)
        per_frame.append(d)
    cap.release()
    if not per_frame:
        return {"ok": False, "ratio": 0.0, "note": "投影点全部落在画面外，标定可能完全不适用"}
    ratio = float(np.median([f["ratio"] for f in per_frame]))
    if ratio >= FIT_RATIO_OK:
        ok, note = True, "投影的球场线与画面白线吻合，标定可用"
    elif ratio >= FIT_RATIO_BAD:
        ok, note = False, ("投影线与画面白线只是弱相关：标定可能有偏差，或镜头在动。"
                           "建议用 calibrate --interactive 重新点角点，"
                           "并对照 --check 输出的叠加图确认")
    else:
        ok, note = False, ("投影线与画面白线基本不相关：这份标定**不适用于这段视频**"
                           "（换过机位 / 分辨率被裁过 / 镜头在动）")
    return {"ok": ok, "ratio": round(ratio, 2), "frames": per_frame, "note": note}


# --------------------------------------------------------------------------
# 2b) 自动精修标定（不需要关键点模型、不需要联网、不需要标注）
# --------------------------------------------------------------------------
def validate_sliding_with_hoop(sliding: dict, hoop_px, t: float = 0.0,
                               max_dist_m: float = 3.0) -> dict:
    """用**人工标的篮筐**校验逐帧滑动标定 —— 这是自动定位那条路上唯一可用的真值。

    为什么必须有这道关卡：滑动标定的质量分（线拟合 ratio）**会被骗**。
    实测这段 960×544 校园转播：坏到"把篮筐投到 9.2m 外"的那些锚点，
    ratio 反而是 7.17 / 5.17 / 7.88（比对的那些"看着还行"的锚点 1.4~2.9 还高）。
    也就是说 `ratio` 完全不能证明坐标对。而人工标的篮筐是**与拟合无关的真值**：
    把它按同一时刻的 H 投到地面，必须落在真篮筐 (0, ±1.575) 附近。

    实测同一段素材：手标篮筐 (356, 104.9) 经 H 投影落在 (0, ±11.2)，
    离真筐 **9.2~9.7m** → 这份滑动标定整段不可用（战术图坐标全错）。

    返回 ``{"checked": bool, "ok": bool, "dist_m": float, "reason": str}``。
    ``checked=False`` 表示没有可用的真值点（没标过篮筐），此时**不能**认为通过。
    """
    from .court import apply_homography
    from .model import HOOP_LEFT, HOOP_RIGHT
    if not sliding or not (sliding.get("anchors") or []):
        return {"checked": False, "ok": False, "dist_m": None,
                "reason": "没有逐帧滑动标定"}
    if not hoop_px:
        return {"checked": False, "ok": False, "dist_m": None,
                "reason": "这段视频没有人工标的篮筐，无法校验自动标定"}
    try:
        fps = float(sliding.get("fps") or 30.0)
        H = homography_at(sliding, int(round(float(t) * fps)),
                          bool(sliding.get("half_court", True)))
        if H is None:
            return {"checked": False, "ok": False, "dist_m": None,
                    "reason": "该时刻没有可用的单应矩阵"}
        x, y = apply_homography(H, float(hoop_px[0]), float(hoop_px[1]))
    except Exception as e:  # noqa: BLE001
        return {"checked": False, "ok": False, "dist_m": None,
                "reason": f"校验失败：{type(e).__name__}: {e}"}
    d = min(math.hypot(x - HOOP_LEFT[0], y - HOOP_LEFT[1]),
            math.hypot(x - HOOP_RIGHT[0], y - HOOP_RIGHT[1]))
    ok = d <= max_dist_m
    return {"checked": True, "ok": bool(ok), "dist_m": round(float(d), 2),
            "court_xy": [round(x, 2), round(y, 2)],
            "hoop_px": [round(float(hoop_px[0]), 1), round(float(hoop_px[1]), 1)],
            "t": round(float(t), 2),
            "reason": ("人工标的篮筐经这份逐帧标定投到 (%0.1f, %0.1f)，"
                       "离真篮筐 %.2f m" % (x, y, d))
                      + ("（在 %.0fm 容差内，通过）" % max_dist_m if ok else
                         "（超过 %.0fm 容差）—— 这份自动逐帧标定不能用它的"
                         "球员坐标出战术图，请手工标定这个机位" % max_dist_m)}


def _corner_pixels(cal: Calibration) -> list:
    return [[float(p[0]), float(p[1])] for p in cal.src_px]


def _ratio_of(bgr, corners, half_court: bool, cal_proto: Calibration) -> float:
    """给一组角点，算它在这一帧上的线拟合比值（越大越准）。"""
    from .court import find_homography, HALF_COURT_CORNERS, FULL_COURT_CORNERS
    dst = HALF_COURT_CORNERS if half_court else FULL_COURT_CORNERS
    try:
        H = find_homography(corners, dst)
    except Exception:  # noqa: BLE001  点共线/退化
        return -1.0
    probe = Calibration(src_px=[list(p) for p in corners],
                        dst_m=[list(p) for p in dst], H=H,
                        frame=cal_proto.frame or "half")
    d = frame_fit_detail(bgr, probe, n_samples=36)
    return float(d["ratio"])


# 标准的"半场广播机位"先验四边形（归一化到画面宽高）。
# 来源：对真实广播帧自动精修收敛后的角点位置做归一化 —— 
# 底线在上方偏中、中线在画面下沿附近，是这个机位的典型构图。
TEMPLATE_QUAD = [(0.073, 0.601), (0.877, 0.489), (0.970, 0.997), (0.079, 1.043)]


def _quad_candidates(shape, hints=None, scales=(0.65, 0.8, 1.0, 1.25, 1.5),
                     shifts=None) -> list:
    """生成一批候选初始四边形（供无先验的自动标定用）。

    思路：球场在画面里的位置/大小是个**低维**问题 —— 先验告诉我们它大概
    占多大、在哪一带，剩下的用"多尺度 × 平移"的小网格覆盖掉就够了。
    每个候选只要 ~3ms 就能打分，几百个候选还不到 1 秒。
    """
    import numpy as np
    h, w = shape[:2]
    if shifts is None:
        shifts = [(-0.15, -0.10), (0.0, -0.10), (0.15, -0.10),
                  (-0.15, 0.0), (0.0, 0.0), (0.15, 0.0),
                  (-0.15, 0.10), (0.0, 0.10), (0.15, 0.10)]
    out = []
    if hints:
        for c in hints:
            if c is not None and len(getattr(c, "src_px", []) or []) == 4:
                out.append([[float(a), float(b)] for a, b in c.src_px])
    base = np.array(TEMPLATE_QUAD, dtype=np.float64)
    cx, cy = base[:, 0].mean(), base[:, 1].mean()
    for sc in scales:
        for (dx, dy) in shifts:
            q = []
            for (nx, ny) in base:
                q.append([(cx + (nx - cx) * sc + dx) * w,
                          (cy + (ny - cy) * sc + dy) * h])
            out.append(q)
    return out


def auto_calibrate(bgr, hints=None, half_court: bool = True,
                   top_k: int = 4, quick_score: int = 14,
                   report: bool = False) -> dict:
    """**无需人工点选**的自动标定：粗搜候选 -> 精修最优的几个 -> 取最好。

    返回 {"corners", "ratio", "ok", "tried", "note"}
    ok 的判据就是 :data:`FIT_RATIO_OK` —— 找不到达标的解就明确说"没找到"，
    而不是随便返回一个看起来像球场的四边形。
    """
    import numpy as np
    cands = _quad_candidates(bgr.shape, hints)
    scored = []
    for q in cands:
        # 粗筛：采样点少一点，快
        try:
            from .court import find_homography, HALF_COURT_CORNERS, FULL_COURT_CORNERS
            dst = HALF_COURT_CORNERS if half_court else FULL_COURT_CORNERS
            # 约束：面积别太离谱（防止"四边形退化成一条线"这种退化解拿高分）。
            # 用鞋带公式算面积 —— 注意 numpy 2.x 的 np.cross 不支持 2D 向量，
            # 之前用它算面积直接抛异常，被下面的 except 吞掉，结果所有候选
            # 都被静默跳过（表现为"没有可用的候选四边形"）。教训：宽 except
            # 一定要留一条能看见原因的路径。
            arr = np.array(q, dtype=np.float64)
            area = 0.5 * abs(float(np.dot(arr[:, 0], np.roll(arr[:, 1], -1))
                                   - np.dot(arr[:, 1], np.roll(arr[:, 0], -1))))
            if area < 0.10 * bgr.shape[0] * bgr.shape[1]:
                continue
            H = find_homography(q, dst)
            probe = Calibration(src_px=[list(p) for p in q],
                                dst_m=[list(p) for p in dst], H=H,
                                frame="half" if half_court else "full")
            d = frame_fit_detail(bgr, probe, n_samples=quick_score)
            scored.append((float(d["ratio"]), q))
        except Exception:  # noqa: BLE001
            continue
    if not scored:
        return {"corners": [], "ratio": 0.0, "ok": False, "tried": 0,
                "note": "没有可用的候选四边形"}
    scored.sort(key=lambda t: -t[0])
    best = {"ratio": -1.0, "corners": None}
    for _r, q in scored[:top_k]:
        proto = Calibration(src_px=[list(p) for p in q],
                            dst_m=[list(p) for p in
                                   (HALF_COURT_CORNERS if half_court
                                    else FULL_COURT_CORNERS)],
                            H=None, frame="half" if half_court else "full")
        try:
            ref = refine_calibration(bgr, proto, half_court=half_court)
        except Exception:  # noqa: BLE001
            continue
        if ref.get("after", 0) > best["ratio"]:
            best = {"ratio": ref["after"], "corners": ref["corners"]}
    ok = best["ratio"] >= FIT_RATIO_OK
    note = ("自动标定成功（线拟合比值 %.2f）" % best["ratio"]) if ok else \
        ("自动标定未达标（最好只有 %.2f，需要 ≥%.1f）：这段画面里球场线不够清晰/"
         "机位不固定/构图与先验差太远，建议改用 calibrate --interactive 手点"
         % (best["ratio"], FIT_RATIO_OK))
    if report:
        print("[自动标定] 候选 %d 个，精修 top%d，最好 ratio=%.2f -> %s"
              % (len(scored), min(top_k, len(scored)), best["ratio"],
                 "可用" if ok else "不可用"))
    return {"corners": best["corners"] or [], "ratio": round(best["ratio"], 2),
            "ok": bool(ok), "tried": len(scored), "note": note}


def refine_calibration(bgr, cal: Calibration, half_court: bool = True,
                       steps: Sequence[int] = (48, 24, 12, 6, 3, 2),
                       report: bool = False) -> dict:
    """用"投影线压在白线上"的评分做**模式搜索**，把 4 个角点精修到位。

    为什么这一步很值：
      * 手点角点误差 10~20px 是常态，而 20px 能让三分线整体偏 1m；
      * 有了客观评分函数，就可以让机器去找"让球场线真正压在画面白线上"的角点 ——
        **不需要球场关键点模型、不需要标注数据、不需要联网**。
    做法：坐标轮换模式搜索（每次只动 8 个坐标里的一个，步长逐级减半）。
    简单、可复现、不会跑飞；每步都只接受"评分变高"的改动，所以只会变好。

    返回 {"corners": [[x,y]...], "before": r0, "after": r1, "improved": bool}
    """
    corners = _corner_pixels(cal)
    if len(corners) != 4:
        return {"corners": corners, "before": 0.0, "after": 0.0,
                "improved": False, "note": "不是 4 角点标定，跳过精修"}
    best = _ratio_of(bgr, corners, half_court, cal)
    r0 = best
    for step in steps:
        improved_any = True
        while improved_any:
            improved_any = False
            for i in range(4):
                for k in (0, 1):
                    for d in (step, -step):
                        trial = [c[:] for c in corners]
                        trial[i][k] += d
                        r = _ratio_of(bgr, trial, half_court, cal)
                        if r > best + 1e-6:
                            best, corners = r, trial
                            improved_any = True
    out = {"corners": corners, "before": round(r0, 3),
           "after": round(best, 3), "improved": best > r0 + 0.05}
    if report:
        print(f"[精修] 线拟合比值 {r0:.2f} -> {best:.2f}")
    return out


def sliding_calibration(video_path: str, cal0: Optional[Calibration] = None,
                        half_court: bool = True, stride: int = 5,
                        max_seconds: float = 0.0, recheck_below: float = 1.4,
                        good_ratio: float = 2.2, min_refine_interval: float = 1.0,
                        report: bool = False,
                        progress=None, job=None) -> dict:
    """**逐帧滑动标定** —— 不训练模型也能跟住会动的镜头。

    为什么必须逐帧：实测这批真实素材里，即便号称"固定机位"的那段，45 秒也漂了
    约 90px；一份静态标定几十秒就失效（边线先跑掉、底线还亮着，只看"有没有压到
    白线"会误判）。而每帧从头自动标定要 5 秒，跑不动。

    三层成本控制（这是能跑起来的关键）：
      1. **分数门控**：先花 ~3ms 量一下"当前这组角点还准不准"。准就直接沿用，
         不精修 —— 镜头没动的那些时间几乎零成本。
      2. **热启动精修**：分数掉下来了，从上一组角点出发只走小步长（6,3），
         0.1~0.3s 收敛；并且限制**最多每秒精修一次**，避免抖动时反复烧时间。
      3. **重新锁死**：分数跌破 recheck_below（镜头跳了/切了），才做一次完整
         自动标定（多尺度候选 + 精修，约 5s）。

    锚点之间线性插值 4 个角点 —— 镜头是连续运动的，帧间变化很小。

    返回 {"anchors":[{frame,corners,ratio,reused}...], "fps","stride",
          "median_ratio","ok_ratio","full_scans","hot_refines","reuses"}
    """
    import cv2
    import numpy as np
    from .court import find_homography
    _require_cv()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"打不开视频：{video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if max_seconds and max_seconds > 0:
        total = min(total, int(max_seconds * fps))
    dst = HALF_COURT_CORNERS if half_court else FULL_COURT_CORNERS

    def proto_of(corners):
        return Calibration(src_px=[list(map(float, c)) for c in corners],
                           dst_m=[list(p) for p in dst],
                           H=find_homography(corners, dst),
                           frame="half" if half_court else "full")

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ok, frame0 = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("读不到第一帧")
    corners = None
    if cal0 is not None and len(getattr(cal0, "src_px", []) or []) == 4:
        c0 = [[float(a), float(b)] for a, b in cal0.src_px]
        if _ratio_of(frame0, c0, half_court, cal0) >= recheck_below:
            corners = c0
    if corners is None:
        r0 = auto_calibrate(frame0, hints=[cal0] if cal0 else None,
                            half_court=half_court)
        corners = r0["corners"] or None
    if not corners:
        cap.release()
        return {"anchors": [], "fps": fps, "stride": stride,
                "note": "第一帧自动标定失败，滑动标定无法启动"}
    proto = proto_of(corners)
    r = _ratio_of(frame0, corners, half_court, proto)
    anchors = [{"frame": 0, "corners": [list(map(float, c)) for c in corners],
                "ratio": round(float(r), 2), "reused": False, "kind": "init"}]
    stats = {"hot_refines": 0, "full_scans": 0, "reuses": 0}
    last_refine_t = 0.0

    fi = stride
    while fi < total:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, fr = cap.read()
        if not ok:
            break
        t = fi / fps
        cur = _ratio_of(fr, corners, half_court, proto)      # 便宜，~3ms
        kind = "reuse"
        if cur >= good_ratio:
            kind = "reuse"
        elif cur < recheck_below:
            full = auto_calibrate(fr, hints=[proto], half_court=half_court)
            if full.get("corners") and full.get("ratio", 0) > cur:
                corners, cur, kind = full["corners"], full["ratio"], "full"
                stats["full_scans"] += 1
            else:
                kind = "reuse"
        elif t - last_refine_t >= min_refine_interval:
            hot = refine_calibration(fr, proto, half_court=half_court, steps=(6, 3))
            if hot.get("after", 0) > cur + 0.02:
                corners, cur, kind = hot["corners"], hot["after"], "hot"
                stats["hot_refines"] += 1
        stats["reuses" if kind == "reuse" else
              ("hot_refines" if kind == "hot" else "full_scans")] += 0
        if kind != "reuse":
            last_refine_t = t
            proto = proto_of(corners)
        else:
            stats["reuses"] += 1
        anchors.append({"frame": int(fi),
                        "corners": [list(map(float, c)) for c in corners],
                        "ratio": round(float(cur), 2), "kind": kind})
        if report and len(anchors) % 40 == 0:
            print(f"    滑动标定 {fi}/{total} ratio={cur:.2f} kind={kind}")
        if progress:
            progress(fi / max(1, total), f"滑动标定 {fi}/{total}")
        fi += stride
    cap.release()
    rs = [x["ratio"] for x in anchors]
    out = {"anchors": anchors, "fps": fps, "stride": stride,
           "total_frames": total, "half_court": half_court,
           "median_ratio": round(float(np.median(rs)), 2) if rs else 0.0,
           "ok_ratio": round(float(np.mean([x >= FIT_RATIO_OK for x in rs])), 3) if rs else 0.0}
    out.update(stats)
    return out


def corners_at(sliding: dict, frame_idx: int) -> Optional[list]:
    """取任意帧的 4 个角点（锚点之间线性插值）。"""
    A = (sliding or {}).get("anchors") or []
    if not A:
        return None
    if frame_idx <= A[0]["frame"]:
        return A[0]["corners"]
    if frame_idx >= A[-1]["frame"]:
        return A[-1]["corners"]
    for i in range(len(A) - 1):
        a, b = A[i], A[i + 1]
        if a["frame"] <= frame_idx <= b["frame"]:
            span = max(1, b["frame"] - a["frame"])
            t = (frame_idx - a["frame"]) / span
            return [[a["corners"][k][0] + (b["corners"][k][0] - a["corners"][k][0]) * t,
                     a["corners"][k][1] + (b["corners"][k][1] - a["corners"][k][1]) * t]
                    for k in range(4)]
    return A[-1]["corners"]


def homography_at(sliding: dict, frame_idx: int, half_court: bool = True):
    """取任意帧的单应矩阵（像素 -> 标定目标坐标）。"""
    c = corners_at(sliding, frame_idx)
    if not c:
        return None
    from .court import find_homography
    try:
        return find_homography(c, HALF_COURT_CORNERS if half_court else FULL_COURT_CORNERS)
    except Exception:  # noqa: BLE001
        return None


def calibration_from_corners(corners, half_court: bool = True,
                             video_path: str = "") -> Calibration:
    """用一组角点直接生成标定（供自动精修后落盘）。"""
    from .court import calibrate_from_corners
    return calibrate_from_corners(corners, half_court=half_court,
                                  video_path=video_path)


# --------------------------------------------------------------------------
# 3) 叠加图（给人看的证据）
# --------------------------------------------------------------------------
def render_overlay(video_path: str, cal: Calibration, t: float,
                   out_path: str) -> Optional[str]:
    """把球场要素投影并画到视频帧上，存成图片。"""
    _require_cv()
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
    ok, img = cap.read()
    cap.release()
    if not ok:
        return None
    W = COURT_WIDTH / 2

    def draw(pts, color, thick=2, closed=False):
        px = [cal.to_pixel(x, y) for x, y in pts]
        if any(not (-1e4 < a < 1e4 and -1e4 < b < 1e4) for a, b in px):
            return
        arr = np.array([[int(round(a)), int(round(b))] for a, b in px], np.int32)
        cv2.polylines(img, [arr], closed, color, thick, cv2.LINE_AA)

    draw([(-W, 0), (W, 0)], (0, 255, 255), 3)
    draw([(-W, -14), (W, -14)], (255, 0, 255), 3)
    draw([(-W, 0), (-W, -14)], (0, 255, 0), 2)
    draw([(W, 0), (W, -14)], (0, 255, 0), 2)
    draw([(-2.45, 0), (-2.45, -5.8), (2.45, -5.8), (2.45, 0)], (255, 128, 0), 2)
    arc = []
    for k in range(0, 181):
        a = math.radians(k)
        arc.append((6.75 * math.sin(a), -1.575 - 6.75 * math.cos(a)))
    draw([p for p in arc if -W - 0.5 <= p[0] <= W + 0.5 and p[1] <= 0.05],
         (0, 0, 255), 2)
    rx, ry = cal.to_pixel(0, -1.575)
    cv2.circle(img, (int(rx), int(ry)), 9, (255, 0, 255), 2)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(out_path, img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return out_path


# --------------------------------------------------------------------------
# 4) 交互式标定（点 4 个角点）
# --------------------------------------------------------------------------
def interactive_calibrate(video_path: str, out_json: str, half_court: bool = True,
                          at_second: float = 0.0, window: str = "aihoop-calibrate"
                          ) -> Optional[Calibration]:
    """在视频帧上点 4 个角点完成标定（OpenCV 窗口）。

    点选顺序（脚本会在画面上提示）：**底线左 → 底线右 → 中线右 → 中线左**。
    按 r 重来，按 q 取消，点满 4 个点自动保存。

    为什么要交互式：角点像素坐标靠人肉估读误差极大（差 20px 就可能让三分线
    整体偏 1m）。点选 + 立刻用 court_fit_score 复核，才是可用的流程。
    """
    _require_cv()
    import cv2
    from .court import calibrate_from_corners

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(at_second * fps))
    ok, img = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"读不到视频帧：{video_path}")

    pts: list = []
    base = img.copy()
    tips = ["底线左", "底线右", "中线右", "中线左"]

    def redraw():
        view = base.copy()
        cv2.putText(view, "click: " + " -> ".join(
            tips[len(pts):] or ["done, press s to save"]),
            (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2, cv2.LINE_AA)
        cv2.putText(view, "r=restart  q=cancel  s=save",
                    (12, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 255, 255), 1, cv2.LINE_AA)
        for i, p in enumerate(pts):
            cv2.circle(view, p, 6, (0, 0, 255), -1)
            cv2.putText(view, str(i + 1), (p[0] + 8, p[1] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        cv2.imshow(window, view)

    def on_click(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN and len(pts) < 4:
            pts.append((float(x), float(y)))
            redraw()

    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window, on_click)
    redraw()
    result = None
    while True:
        k = cv2.waitKey(20) & 0xFF
        if k in (ord("q"), 27):
            break
        if k == ord("r"):
            pts.clear()
            redraw()
        if k == ord("s") and len(pts) == 4:
            cal = calibrate_from_corners(pts, half_court=half_court,
                                         video_path=video_path)
            cal.save(out_json)
            result = cal
            break
        if len(pts) == 4 and result is None:
            # 点满 4 个点先给个预览，再等 s 保存
            pass
    cv2.destroyWindow(window)
    return result