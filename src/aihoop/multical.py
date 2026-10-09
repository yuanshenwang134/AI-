"""多镜头标定：**两台机位各解一份单应矩阵**，再在结果层合并。

为什么必须这样（用户实测的根本需求）
------------------------------------
用户的原话："一个镜头怎么可能看到全场啊，就是两个镜头标点，按照标出来的点
算战术图和投篮热区啊"。

这是对的。一台机位拍不到全场，所以真实工作流是：
    镜头 A → 覆盖半场 A        镜头 B → 覆盖半场 B
两个机位**没有共同坐标系**，把它们的点混在一起解**一个**单应矩阵在数学上
不可能成立（用户反复看到的"解算失败 / 点的位置对不上"就是这么来的）。

正确做法：
    镜头 A 自己解一份 H_A → A 覆盖的那部分投到球场坐标
    镜头 B 自己解一份 H_B → B 覆盖的那部分投到球场坐标
               ↓ 在**结果层**合并（都是球场坐标了，直接叠）
           一张全场战术图 / 热区图

本模块负责：
  * `Segment` / `MultiCal`：把"每个镜头一份标定 + 它覆盖的时间段"存下来；
  * `solve_per_shot()`：按镜头分组、各自解算（**绝不跨镜头合并**）；
  * `plan_segments()`：用切镜时刻把时间轴切开，算出每段的范围。

分析端用 `MultiCal.cal_at(t)` 取该帧所属镜头的标定即可（见 sources.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .court import Calibration


@dataclass
class Segment:
    """一个镜头一段时间的标定。"""
    t_start: float
    t_end: float
    src_px: list = field(default_factory=list)
    dst_m: list = field(default_factory=list)
    H: list = field(default_factory=list)
    frame: str = "full"            # full | half
    names: list = field(default_factory=list)
    rmse_m: float = 0.0
    ratio: float = 0.0             # 吻合度（投影线压在白线上的比例）
    method: str = "web-keypoints-multi"
    n_points: int = 0
    note: str = ""

    def calibration(self, for_video: str = "", frame_size=None) -> Calibration:
        cal = Calibration(name="seg", method=self.method, src_px=self.src_px,
                          dst_m=self.dst_m, H=self.H, reproj_error_m=self.rmse_m,
                          frame=self.frame, for_video=for_video,
                          frame_size=list(frame_size or []))
        try:
            cal.point_names = list(self.names)          # type: ignore[attr-defined]
        except Exception:                               # noqa: BLE001
            pass
        return cal

    def to_dict(self) -> dict:
        return {"t_start": round(float(self.t_start), 3),
                "t_end": round(float(self.t_end), 3),
                "src_px": self.src_px, "dst_m": self.dst_m, "H": self.H,
                "frame": self.frame, "names": list(self.names),
                "rmse_m": round(float(self.rmse_m), 4),
                "ratio": round(float(self.ratio), 3),
                "method": self.method, "n_points": int(self.n_points),
                "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        src = list(d.get("src_px") or [])
        dst = list(d.get("dst_m") or [])
        names = list(d.get("names") or [])
        H = d.get("H") or []
        rmse = float(d.get("rmse_m") or 0.0)
        note = d.get("note") or ""

        # Older web calibration files included the hoop center among ground-plane
        # landmarks. It is 3.05m above the court, so that correspondence bends the
        # homography badly (often visible as players collapsing into a corner after
        # a camera cut). Drop those legacy points and rebuild H from the remaining
        # ground points when there are enough correspondences.
        if names and len(names) == len(src) == len(dst) and any(
                str(name).startswith("hoop") for name in names):
            keep = [j for j, name in enumerate(names)
                    if not str(name).startswith("hoop")]
            if len(keep) >= 4:
                src = [src[j] for j in keep]
                dst = [dst[j] for j in keep]
                names = [names[j] for j in keep]
                try:
                    from .court import apply_homography, find_homography
                    H = find_homography(src, dst)
                    errors = []
                    for (px, py), (x, y) in zip(src, dst):
                        u, v = apply_homography(H, float(px), float(py))
                        errors.append(((u - float(x)) ** 2 + (v - float(y)) ** 2) ** 0.5)
                    rmse = sum(errors) / len(errors)
                    note = (note + "; " if note else "") + \
                        "legacy hoop landmark removed; ground-plane H rebuilt"
                except (ValueError, ZeroDivisionError):
                    pass

        return cls(t_start=float(d.get("t_start") or 0.0),
                   t_end=float(d.get("t_end") or 0.0),
                   src_px=src, dst_m=dst, H=H, frame=d.get("frame") or "full",
                   names=names, rmse_m=rmse,
                   ratio=float(d.get("ratio") or 0.0),
                   method=d.get("method") or "web-keypoints-multi",
                   n_points=len(src), note=note)


class MultiCal:
    """一台机位一段标定，合起来覆盖整段视频。

    没有 `segments` 时它退化成"只有一段"，所以调用方可以无脑用它。
    """

    def __init__(self, segments: list, for_video: str = "",
                 frame_size: Optional[list] = None, note: str = ""):
        self.segments = [s for s in (segments or []) if s and s.H]
        self.segments.sort(key=lambda s: s.t_start)
        self.for_video = for_video or ""
        self.frame_size = list(frame_size or [])
        self.note = note

    # ---------------------------------------------------------------- 查询
    def segment_at(self, t: float) -> Optional[Segment]:
        """取 t 时刻所属的镜头段；落在空隙里时取**最近**的一段。

        边界（正好等于切镜时刻）归**后一段** —— 那一帧已经是新镜头了。
        所以这里倒序找，让后一段优先命中。
        """
        if not self.segments:
            return None
        t = float(t)
        for s in reversed(self.segments):
            if s.t_start <= t <= s.t_end:
                return s
        # 落在两段之间（或视频首尾之外）→ 取时间上最近的一段
        return min(self.segments,
                   key=lambda s: min(abs(t - s.t_start), abs(t - s.t_end)))

    def cal_at(self, t: float, for_video: str = "") -> Optional[Calibration]:
        s = self.segment_at(t)
        if s is None:
            return None
        return s.calibration(for_video=self.for_video or for_video,
                             frame_size=self.frame_size)

    @property
    def primary(self) -> Optional[Segment]:
        """点数最多的那段 —— 用来做展示/叠加（如篮筐投影、画面回投线）。"""
        if not self.segments:
            return None
        return max(self.segments, key=lambda s: (s.n_points, s.ratio))

    def to_court(self, t: float, x: float, y: float):
        cal = self.cal_at(t)
        if cal is None:
            return None
        return cal.to_court(x, y)

    def to_pixel(self, t: float, x: float, y: float):
        cal = self.cal_at(t)
        if cal is None:
            return None
        return cal.to_pixel(x, y)

    def in_court(self, t: float, x: float, y: float, tol: float = 0.0) -> bool:
        cal = self.cal_at(t)
        if cal is None:
            return False
        try:
            return bool(cal.in_court(x, y, tol=tol))
        except TypeError:                    # 兼容不同签名
            return bool(cal.in_court(x, y, tol))

    def matches_video(self, video_path: str, W: int, H: int) -> bool:
        for s in self.segments:
            try:
                if s.calibration(for_video=self.for_video,
                                 frame_size=self.frame_size).matches_video(
                                     video_path, W, H):
                    return True
            except Exception:                # noqa: BLE001
                continue
        return False

    # ---------------------------------------------------------------- 序列化
    def to_dict(self) -> dict:
        """融进现有的 `calibration_<stem>.json` ——
        顶层仍然写一份"主标定"的字段（老代码/老界面读得懂），
        另外加 `segments` 给新代码用。"""
        p = self.primary
        out: dict = {}
        if p is not None:
            base = p.calibration(for_video=self.for_video,
                                 frame_size=self.frame_size)
            out = dict(getattr(base, "__dict__", {}) or {})
        out["multi_shot"] = True
        out["segments"] = [s.to_dict() for s in self.segments]
        out["for_video"] = self.for_video
        out["frame_size"] = self.frame_size
        if self.note:
            out["multi_note"] = self.note
        if p is not None:
            out["reproj_error_m"] = p.rmse_m
            out["point_names"] = list(p.names)
        return out

    @classmethod
    def from_dict(cls, d: dict) -> Optional["MultiCal"]:
        segs = d.get("segments")
        if not segs:
            return None
        return cls([Segment.from_dict(x) for x in segs],
                   for_video=d.get("for_video") or "",
                   frame_size=d.get("frame_size") or [],
                   note=d.get("multi_note") or "")

    # ---------------------------------------------------------------- 概览
    def summary(self) -> dict:
        return {"multi_shot": True, "n_segments": len(self.segments),
                "segments": [{"t_start": s.t_start, "t_end": s.t_end,
                              "n_points": s.n_points, "rmse_m": s.rmse_m,
                              "ratio": s.ratio, "frame": s.frame}
                             for s in self.segments]}


# --------------------------------------------------------------- 质量判读
# 多机位下"**最差的一段**"才是这份标定的真实质量。
# 为什么单独立成函数（实测事故）：用户那次两段是
#     段1（0~54s）  rmse=0.97  **ratio=0.34**   ← 投影线与画面白线基本不相关
#     段2（54~240s）rmse=5.80  ratio=3.12
# 而界面只读"主标定"那一段、报告取 `max(ratio)` → 报 3.12，
# **"半个视频坐标全错"完全被盖住**，战术图看着"可用"其实一半的点是错的。
# 所以判据只写这一处，api 和 sources 都调它，避免两边不一致。
RMSE_MAX = 1.5          # 平均重投影误差上限（米）
RATIO_MIN = 1.25        # 投影线与画面白线的吻合度下限（与分析端同一道门槛）


def segment_is_weak(seg, rmse_max: float = RMSE_MAX,
                    ratio_min: float = RATIO_MIN) -> str:
    """这一段弱在哪；没问题返回空串。"""
    if float(getattr(seg, "rmse_m", 0.0) or 0.0) > rmse_max:
        return "重投影误差偏大"
    r = getattr(seg, "ratio", None)
    if r is not None and float(r or 0.0) < ratio_min:
        return "吻合度过低（投影线与画面白线对不上）"
    return ""


def weak_segments(mc, rmse_max: float = RMSE_MAX,
                  ratio_min: float = RATIO_MIN) -> list:
    """列出所有不可信的段（空列表 = 每一段都可信）。"""
    out = []
    for s in (getattr(mc, "segments", None) or []):
        why = segment_is_weak(s, rmse_max, ratio_min)
        if why:
            out.append({"t_start": s.t_start, "t_end": s.t_end,
                        "rmse_m": s.rmse_m, "ratio": s.ratio, "why": why})
    return out


def worst_readings(mc) -> dict:
    """多机位标定的"真实质量" = **最差**那一段的读数。

    不能取 `max(ratio)`：那会把坏段盖住（实测 0.34 / 3.12 → 报 3.12）。
    """
    segs = list(getattr(mc, "segments", None) or [])
    if not segs:
        return {"rmse_m": None, "ratio": None}
    ratios = [float(s.ratio) for s in segs if s.ratio is not None]
    return {"rmse_m": round(max(float(s.rmse_m or 0.0) for s in segs), 3),
            "ratio": (round(min(ratios), 3) if ratios else None)}


# ---------------------------------------------------------------------- 切段
def plan_segments(duration: float, cuts: list, marks: list,
                  pad: float = 0.0) -> list:
    """按切镜时刻把 [0, duration] 切成若干段。

    marks 是用户标点的时刻列表；切出来的段会被**收拢到**包含 mark 的那些段，
    没用到的段直接丢掉 —— 用户没标过的镜头，我们没有它的标定，留着没意义。

    返回 [(t_start, t_end, [marks...]), ...]
    """
    d = max(0.0, float(duration or 0.0))
    cs = sorted({round(float(c), 3) for c in (cuts or []) if 0 < float(c) < d})
    bounds = [0.0] + cs + [d]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        inside = [float(m) for m in (marks or []) if a - 1e-6 <= float(m) <= b + 1e-6]
        if inside:
            out.append((max(0.0, a - pad), min(d, b + pad), inside))
    return out


# ------------------------------------------------------------------ 切镜定位
def _sample(video: str, times: list):
    """**一次打开视频**，批量取多个时刻的降采样灰度图 + 色相直方图。

    为什么必须批量（实测 7.2s → 0.5s）：原来每个时刻都新开一个
    `cv2.VideoCapture` 再 seek，实测单次 0.335s —— 22 个采样点就是 7.4 秒，
    而瓶颈完全在"反复打开视频"上，不在算法。用户的素材是长视频，seek 更贵。
    返回 [(gray, hist) 或 None, ...]，与 times 一一对应。
    """
    try:
        import cv2
        import numpy as np                          # noqa: F401
    except ImportError:
        return [None] * len(times)
    out = []
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return [None] * len(times)
    try:
        # 按时间升序 seek，尽量让底层顺序前进（随机 seek 在长视频上很贵）
        order = sorted(range(len(times)), key=lambda i: float(times[i]))
        got = {}
        for i in order:
            t = max(0.0, float(times[i]))
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, fr = cap.read()
            if not ok or fr is None:
                got[i] = None
                continue
            h, w = fr.shape[:2]
            if w > 480:                # 降采样：HD 素材省掉 ~80% 像素
                fr = cv2.resize(fr, (480, max(1, int(h * 480.0 / w))),
                                interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)], [0],
                                None, [180], [0, 180])
            cv2.normalize(hist, hist)
            got[i] = (gray, hist, fr)
        for i in range(len(times)):
            out.append(got.get(i))
    finally:
        cap.release()
    return out


def _pair_diff(sa, sb):
    """两帧的 (corr, mad)；任一为空则返回 None。"""
    if sa is None or sb is None:
        return None
    import cv2
    gray_a, hist_a = sa[0], sa[1]
    gray_b, hist_b = sb[0], sb[1]
    if gray_a.shape != gray_b.shape:
        return None
    mad = float(cv2.absdiff(gray_a, gray_b).mean())
    corr = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))
    return corr, mad


def _orb_inliers(sa, sb, max_side: int = 320) -> Optional[int]:
    """两帧的 ORB 几何一致内点数；拿不到特征时返回 None。

    为什么最终还是要用 ORB 判切镜（实测教训）：
      MAD 在真实转播素材上**太吵** —— 同一镜头相隔 1.5 秒的两帧（快攻+摇镜）
      MAD 能到 54，而不同镜头才 82，两者区间重叠，固定阈值和"中位数倍数"
      的自适应阈值都分不开（实测两次都判错）。
      切镜的本质是**几何不连续**：摇镜再快也能对上特征，切镜对不上。
      所以判据用内点率，而不是像素差。
      配上"批量采样 + 降采样到 320"之后 ORB 是毫秒级，不再拖垮接口。
    """
    if sa is None or sb is None:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    ga, gb = sa[0], sb[0]
    if ga is None or gb is None or ga.shape != gb.shape:
        return None
    h, w = ga.shape[:2]
    sc = max(1.0, max(h, w) / float(max_side))
    if sc > 1.0:
        size = (max(8, int(w / sc)), max(8, int(h / sc)))
        ga = cv2.resize(ga, size, interpolation=cv2.INTER_AREA)
        gb = cv2.resize(gb, size, interpolation=cv2.INTER_AREA)
    try:
        orb = cv2.ORB_create(600)
        ka, da = orb.detectAndCompute(ga, None)
        kb, db = orb.detectAndCompute(gb, None)
        if da is None or db is None or len(ka) < 8 or len(kb) < 8:
            return None
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        ms = bf.match(da, db)
        if len(ms) < 8:
            return 0
        ms = sorted(ms, key=lambda m: m.distance)[:120]
        pts_a = np.float32([ka[m.queryIdx].pt for m in ms]).reshape(-1, 1, 2)
        pts_b = np.float32([kb[m.trainIdx].pt for m in ms]).reshape(-1, 1, 2)
        _H, mask = cv2.findHomography(pts_a, pts_b, cv2.RANSAC, 4.0)
        if mask is None:
            return 0
        return int(mask.sum())
    except Exception:                        # noqa: BLE001
        return None


def _cut_score(sa, sb):
    """越大越像"切镜"。**内点率反着算** —— 内点越少越可能是切镜。

    内点够多 → 两帧几何一致 → 同一镜头（返回 0）。
    内点很少 → 几何对不上 → 切镜（返回很大的分）。
    拿不到特征（特征太少，如特写/模糊）→ 退回 MAD 当参考。
    """
    inl = _orb_inliers(sa, sb)
    if inl is not None:
        if inl >= 20:
            return 0.0                       # 几何一致，铁定同一镜头
        if inl <= 5:
            return 100.0                     # 几何完全对不上，铁定切镜
        return float(100 - inl * 4)          # 中间地带：内点越少分越高
    d = _pair_diff(sa, sb)
    return float(d[1]) if d else 0.0


def _same_shot(video: str, a: float, b: float) -> bool:
    """两帧是不是同一个镜头。"""
    sa, sb = _sample(video, [a, b])
    return _cut_score(sa, sb) < 50.0


def find_cut_between(video: str, a: float, b: float,
                     coarse: float = 1.5, fine: float = 0.25,
                     budget: int = 40) -> Optional[float]:
    """在 (a, b) 之间找出**切镜时刻**；找不到就返回 None。

    做法：**一次批量采样**整段（降采样图上跑 ORB 判几何一致性），
    找出第一个"几何对不上"的区间，再二分细化。
    """
    a, b = float(a), float(b)
    if b - a < 0.4:
        return None
    if not _same_shot(video, a, b):
        # 两端本身就不同镜头 → 不必扫，直接二分定位
        pass
    n = max(3, min(20, int(round((b - a) / max(0.4, coarse)))))
    ts = [a + (b - a) * i / n for i in range(n + 1)]
    samples = _sample(video, ts)                 # 只开一次视频
    scores = [_cut_score(samples[i], samples[i + 1]) for i in range(n)]
    if not scores:
        return None
    hit = None
    for i, sc in enumerate(scores):
        if sc >= 50.0:
            hit = i
            break
    if hit is None:
        return None
    x, y = ts[hit], ts[hit + 1]
    calls = 0
    while (y - x) > fine and calls <= budget:
        mid = (x + y) / 2.0
        calls += 1
        sa, sb = _sample(video, [x, mid])
        if _cut_score(sa, sb) >= 50.0:
            y = mid                          # x..mid 之间就断了 → 切镜在前半
        else:
            x = mid
    return round((x + y) / 2.0, 2)


def split_times_by_shot(video: str, times: list) -> tuple:
    """把用户标点的时刻按镜头分组。

    返回 (groups, cuts)：
      groups = [[t...], [t...]] —— 每个元素是同一镜头里的一组时刻
      cuts   = [切镜时刻...]
    """
    ts = sorted({round(float(t), 3) for t in (times or [])})
    if not ts:
        return [], []
    groups = [[ts[0]]]
    cuts = []
    for t0, t1 in zip(ts, ts[1:]):
        c = find_cut_between(video, t0, t1)
        if c is None:
            groups[-1].append(t1)            # 没找到切镜 → 同一镜头
            continue
        cuts.append(c)
        groups.append([t1])
    return groups, cuts


# ------------------------------------------------------------------ 分镜解算
def _solve_one(group_frames: list, W: int, H: int):
    """对一个镜头里的点解一份标定。

    group_frames: [{"t":..., "px": [[x,y]...], "dst": [[mx,my]...], "names": [...]}]
    返回 (Segment|None, note)
    """
    import numpy as np

    from .calibcheck import frame_fit_detail
    from .court import find_homography

    src, dst, names = [], [], []
    bgr_ref = None
    for f in group_frames:
        if bgr_ref is None and f.get("bgr") is not None:
            bgr_ref = f["bgr"]
        for p, d, n in zip(f.get("px") or [], f.get("dst") or [],
                           f.get("names") or []):
            src.append(list(p))
            dst.append(list(d))
            names.append(n)
    if len(src) < 4:
        return None, "这一组只有 %d 个点，解不出标定" % len(src)

    via = "dlt"
    try:
        Hm = find_homography(src, dst)
    except Exception:                                   # noqa: BLE001
        import cv2
        try:
            Hr, _mask = cv2.findHomography(
                np.array(src, dtype=np.float64),
                np.array(dst, dtype=np.float64), cv2.RANSAC, 5.0)
            if Hr is None:
                return None, "RANSAC 也没找到一致子集"
            Hm = Hr.tolist()
            via = "ransac"
        except Exception as e:                          # noqa: BLE001
            return None, "解不出单应矩阵（%s: %s）" % (type(e).__name__, e)

    cal = Calibration(name="seg", method="web-keypoints-multi", src_px=src,
                      dst_m=dst, H=Hm, frame="full", frame_size=[W, H])
    rmse = 0.0
    ratio = 0.0
    try:
        cal = cal.fit()
        rmse = float(getattr(cal, "reproj_error_m", 0.0) or 0.0)
    except Exception:                                   # noqa: BLE001
        pass
    if bgr_ref is not None:
        try:
            d = frame_fit_detail(bgr_ref, cal)
            ratio = float(d.get("ratio") or 0.0)
        except Exception:                               # noqa: BLE001
            pass
    seg = Segment(t_start=0.0, t_end=0.0, src_px=src, dst_m=dst, H=list(Hm),
                  frame="full", names=names, rmse_m=rmse, ratio=ratio,
                  method="web-keypoints-multi", n_points=len(src),
                  note="%s 解算，%d 个点" % (via, len(src)))
    return seg, ""


def solve_per_shot(video: str, frames: list, duration: float = 0.0,
                   frame_grab=None) -> dict:
    """**按镜头分别解算**（绝不跨镜头合并）。

    frames: [{"t": 12.3, "px": [[x,y]...], "dst": [[mx,my]...], "names": [...]}]
            px 是**像素**坐标，dst 是球场坐标（米）。
    frame_grab: 可选，callable(t) -> BGR ndarray，用来算吻合度。

    返回 {"ok", "multical", "groups", "cuts", "notes"}
    """
    frames = [f for f in (frames or []) if f.get("px")]
    if not frames:
        return {"ok": False, "multical": None, "groups": [], "cuts": [],
                "notes": ["没有标点数据"]}
    W = int(frames[0].get("W") or 0)
    H = int(frames[0].get("H") or 0)

    times = [float(f["t"]) for f in frames]
    groups, cuts = split_times_by_shot(video, times)

    notes = []
    if len(groups) > 1:
        notes.append("检测到 %d 个镜头（切镜时刻 %s）—— 已**按镜头分别解算**："
                     "每个镜头一份单应矩阵，各自把自己覆盖的部分投到球场坐标后"
                     "再合并。两台机位本来就没有共同坐标系，硬合并成一份必然矛盾。"
                     % (len(groups), "、".join("%.1fs" % c for c in cuts)))
    by_t = {round(float(f["t"]), 3): f for f in frames}
    segs = []
    for gi, g in enumerate(groups):
        gf = []
        for t in g:
            f = by_t.get(round(float(t), 3))
            if not f:
                continue
            f2 = dict(f)
            if frame_grab is not None and f2.get("bgr") is None:
                try:
                    f2["bgr"] = frame_grab(float(t))
                except Exception:                       # noqa: BLE001
                    f2["bgr"] = None
            gf.append(f2)
        seg, why = _solve_one(gf, W, H)
        if seg is None:
            notes.append("第 %d 个镜头（t=%s）：%s"
                         % (gi + 1, "、".join("%.1f" % x for x in g), why))
            continue
        lo = 0.0
        hi = float(duration or 0.0)
        for c in cuts:
            if c <= min(g):
                lo = max(lo, c)
            if c >= max(g):
                hi = min(hi, c) if hi > 0 else c
        if hi <= 0:
            hi = lo + 9999.0
        seg.t_start = round(float(lo), 3)
        seg.t_end = round(float(hi), 3)
        segs.append(seg)
        notes.append("镜头 %d（t=%.1f~%.1fs）：%d 个点，吻合度 %.2f，误差 %.2f m"
                     % (gi + 1, seg.t_start, seg.t_end, seg.n_points,
                        seg.ratio, seg.rmse_m))
    if not segs:
        return {"ok": False, "multical": None, "groups": groups, "cuts": cuts,
                "notes": notes or ["每个镜头都解不出标定"]}
    mc = MultiCal(segs, for_video=video,
                  frame_size=[W, H] if W and H else [],
                  note="；".join(notes))
    return {"ok": True, "multical": mc, "groups": groups, "cuts": cuts,
            "notes": notes}
