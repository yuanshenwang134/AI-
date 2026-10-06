"""球场几何 + 单应矩阵标定。

这一层的作用：把「像素坐标」变成「分析用的球场坐标（米）」。
计分规则引擎要用 6.75m / 0.90m 这些真实尺度判 1/2/3 分，
热区图与**俯视战术图（套餐 B）**也都要把球员画在标准球场上，
所以标定的正确性是整条坐标链的地基。

--------------------------------------------------------------------------
分析坐标系（"折半坐标"）—— 全工程唯一口径
--------------------------------------------------------------------------
  x 横向 ±7.5（x=0 是球场中轴；|x| 越大越贴边线）
  y 纵向：**符号表示在哪半场，|y| 表示离那条底线的距离**
        y < 0 → 左半场（攻左篮筐），|y|=0 是左底线，|y|=14 是中圈
        y > 0 → 右半场（攻右篮筐），|y|=0 是右底线，|y|=14 是中圈
  篮筐 (0, ±1.575)：|1.575| 正好是 FIBA 的「篮筐圆心距底线」——
  只有把 |y| 定义成"离底线的距离"，这个常数才对得上。

半场标定的 4 个目标点（按点击顺序）：
    底线左 (-7.5, 0) → 底线右 (7.5, 0) → 中线右 (7.5, -14) → 中线左 (-7.5, -14)

--------------------------------------------------------------------------
为什么还需要"全场坐标"，以及它怎么变成分析坐标
--------------------------------------------------------------------------
相机拍得下整场时，单应矩阵的 4 组对应点必须用**未折半**的全场坐标
（两条底线在 y=∓14、中线在 y=0），否则同一条对应关系里没法同时表示两个底线。
这类标定记为 ``frame="full"``，它输出的坐标要先经过 :func:`fold_to_analysis`
折到分析坐标系再给下游用（:meth:`Calibration.to_court` 已经把这一步封装好）。

> 折半坐标是**有意的设计**，不是历史包袱：一场比赛里两队各攻一个篮筐，
> 把两个半场叠起来后，热区图 / 分区统计 / 俯视战术图都只需要画一套半场，
> 左右两侧的出手天然对齐。全场坐标只活在标定这一步，不进下游。

--------------------------------------------------------------------------
两种标定方式（按成本从低到高）
--------------------------------------------------------------------------
  1. manual    —— 人工点 4 个角点（最快，2 分钟，demo 完全够用）
  2. keypoints —— 用球场关键点检测模型自动出点，再算单应
两者都收敛到同一个 Homography，下游代码不用改。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from typing import Optional, Sequence

from .model import COURT_LENGTH, COURT_WIDTH


# --------------------------------------------------------------------------
# 单位换算：把「半场长度坐标」换成「全场中心坐标」
# --------------------------------------------------------------------------
def half_to_full(x_half: float, y_half: float) -> tuple[float, float]:
    """把「半场自建坐标」换成分析坐标（折半坐标）。

    x_half ∈ [0, 15] 从左边线量起；y_half ∈ [0, 14] 从**被进攻的那条底线**量起。
    这里约定被进攻的是左半场，所以分析坐标为 (x_half - 7.5, -y_half)。

    > 历史坑：老版本的 y 方向是反的（把底线当成了 |y|=14）。修正之后
    > ``HALF_COURT_CORNERS``、:func:`fold_to_analysis`、前端 court.js 的
    > ``v=|y|``（v=0 是底线）三者口径才完全一致。
    """
    return x_half - COURT_WIDTH / 2, -y_half


def fold_to_analysis(x: float, y: float, mode: str = "full"
                     ) -> tuple[float, float]:
    """把「标定输出坐标」折到分析坐标（|y| = 离本方底线的距离）。

    单应矩阵的输出到底落在哪种坐标里，**取决于当初拿什么点做的标定**，
    一共四种情形（mode）：

      half   目标点本来就是折半分析坐标（底线 |y|=0、中线 |y|=14）→ 原样返回
      left   目标是「未折半的左半场」（底线 y=-14、中线 y=0）→ y_a = -(14 + y)
      right  目标是「未折半的右半场」（底线 y=+14、中线 y=0）→ y_a = 14 - y
      full   目标是真·全场（两条底线 y=±14、中线 y=0）→ 按点所在半场分别折

    为什么要分这么细：老版本的半场标定点（底线→-14、中线→0）与新版本的
    半场标定点（底线→0、中线→-14）**y 的取值集合完全相同**，只是哪个端点是
    底线反了过来。只按点判断会认错半场 —— 实测会得到「底线折到 0、中线折到
    +14」这种自相矛盾的结果，球员位置在战术图上会整体错到另一个半场。

    ``mode=full`` 时本函数自反，可以直接正反两用；其余模式请配合
    :func:`unfold_from_analysis` 用（见 Calibration.to_pixel）。
    """
    if mode in ("half", ""):
        return x, y
    if mode == "left":
        return x, -(COURT_LENGTH / 2 + y)
    if mode == "right":
        return x, COURT_LENGTH / 2 - y
    # full：按点落在哪个半场分别折
    if y >= 0:
        return x, COURT_LENGTH / 2 - y
    return x, -(COURT_LENGTH / 2 + y)


def unfold_from_analysis(x: float, y: float, mode: str = "full"
                         ) -> tuple[float, float]:
    """:func:`fold_to_analysis` 的逆运算（分析坐标 -> 标定输出坐标）。

    画图（把篮筐/网格叠回视频帧）时要走这条路，所以必须严格互逆。
    """
    if mode in ("half", ""):
        return x, y
    if mode == "left":
        return x, -COURT_LENGTH / 2 - y
    if mode == "right":
        return x, COURT_LENGTH / 2 - y
    if y >= 0:
        return x, COURT_LENGTH / 2 - y
    return x, -COURT_LENGTH / 2 - y


def is_valid_court_point(x: float, y: float, tol: float = 2.0) -> bool:
    """分析坐标是否落在球场附近（容忍标定误差）。

    用途：把明显离谱的投影点（标定错机位、球飞出画面）挡在统计之外，
    否则一个 y=40 的点会把俯视战术图整张图拉爆。
    """
    return (abs(x) <= COURT_WIDTH / 2 + tol
            and -COURT_LENGTH / 2 - tol <= y <= COURT_LENGTH / 2 + tol)


# --------------------------------------------------------------------------
# 标定退化检测（近共线 / 点重合）
# --------------------------------------------------------------------------
def max_triangle_area(pts: Sequence[Sequence[float]]) -> float:
    """点集里最大三角形的面积（纯 python）。

    这是"这些点有没有把平面真正撑开"的度量：近共线的点集最大三角形面积趋近 0。
    为什么要它：**重投影误差抓不住退化标定**。4 个点几乎共线时，解出来的 H 在这些
    点上是精确解（误差 0.00m），但离开这条线就会飞掉 —— 实测一份用户标的标定
    4 个点 y 都在 310~325px（几乎一条横线），重投影误差 0.00~1.45m 显示"可用"，
    而画面里的篮筐被投到 (-31.0, -14.0)、离真篮筐 33.4m，热区/战术图整片错位。
    """
    best = 0.0
    n = len(pts or [])
    for i in range(n - 2):
        x0, y0 = float(pts[i][0]), float(pts[i][1])
        for j in range(i + 1, n - 1):
            x1, y1 = float(pts[j][0]), float(pts[j][1])
            for k in range(j + 1, n):
                x2, y2 = float(pts[k][0]), float(pts[k][1])
                a = abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) / 2.0
                if a > best:
                    best = a
    return best


def min_pair_distance(pts: Sequence[Sequence[float]]) -> float:
    """点集里最近两点的距离（点重合 → 0，同样解不出稳定的 H）。"""
    best = float("inf")
    n = len(pts or [])
    for i in range(n - 1):
        for j in range(i + 1, n):
            d = math.hypot(float(pts[i][0]) - float(pts[j][0]),
                           float(pts[i][1]) - float(pts[j][1]))
            if d < best:
                best = d
    return best if n >= 2 else 0.0


def calibration_degeneracy(cal, min_src_frac: float = 0.008,
                           min_dst_frac: float = 0.0025) -> dict:
    """这份标定的点位是不是退化的（近共线 / 重合）？

    为什么必须单独判一次：单应矩阵只需要 4 组点，**4 个点永远能解出一个"误差 0"的
    解**，哪怕其中 3 个点在一条线上。于是"重投影误差很小"根本不能证明标定可用，
    而下游（热区、俯视战术图）会照着一张错的坐标图给出看着很专业的结论。
    这里用**点集的二维展开度**把这种解挡掉：

      * 像素侧：最大三角形面积 < 画面面积的 0.8%（且小于 400px² 的绝对下限）
      * 球场侧：最大三角形面积 < 球场面积的 0.25%（约 1.05m²）
      * 两侧最近两点距离 < 2px / 0.25m（点被重复点到了同一个地方）

    返回 ``{"degenerate": bool, "reason": str, ...质量读数}``。
    读数是给界面/报告用的 —— 说清"为什么不能用"，而不是只给一个 False。
    """
    src = list(getattr(cal, "src_px", None) or [])
    dst = list(getattr(cal, "dst_m", None) or [])
    out = {"degenerate": False, "reason": "", "n_points": len(src),
           "src_tri_px2": round(max_triangle_area(src), 2),
           "dst_tri_m2": round(max_triangle_area(dst), 3),
           "src_min_dist_px": round(min_pair_distance(src), 2),
           "dst_min_dist_m": round(min_pair_distance(dst), 3),
           "src_frac": 0.0, "dst_frac": 0.0}
    if len(src) < 4 or len(dst) != len(src):
        out["degenerate"] = True
        out["reason"] = (f"只有 {len(src)} 组对应点 —— 单应矩阵至少要 4 组，"
                         "而且 4 组时解是精确解、误差恒为 0，无法自证正确。")
        return out
    if not getattr(cal, "H", None):
        out["degenerate"] = True
        out["reason"] = "没有解出单应矩阵。"
        return out

    size = list(getattr(cal, "frame_size", None) or [])
    if len(size) >= 2 and float(size[0]) > 0 and float(size[1]) > 0:
        ref_src = float(size[0]) * float(size[1])
    else:                       # 老文件没有 frame_size：用点的外接框兜底
        xs = [float(p[0]) for p in src]
        ys = [float(p[1]) for p in src]
        ref_src = max(1.0, (max(xs) - min(xs)) * (max(ys) - min(ys)))
    ref_dst = COURT_WIDTH * COURT_LENGTH
    out["src_frac"] = round(out["src_tri_px2"] / ref_src, 5)
    out["dst_frac"] = round(out["dst_tri_m2"] / ref_dst, 5)

    if out["src_min_dist_px"] < 2.0:
        out["degenerate"] = True
        out["reason"] = ("有两个标定点落在同一个位置（距离 %.1fpx）—— "
                         "多半是同一个点被点了两次。" % out["src_min_dist_px"])
    elif out["dst_min_dist_m"] < 0.25:
        out["degenerate"] = True
        out["reason"] = ("有两个标定点对应到球场上同一个位置（相距 %.2fm）—— "
                         "点位名称选重了。" % out["dst_min_dist_m"])
    elif (out["src_tri_px2"] < max(400.0, min_src_frac * ref_src)
          and out["src_frac"] < min_src_frac):
        out["degenerate"] = True
        out["reason"] = ("标定点几乎在**同一条线**上（最大三角形面积只有 "
                         "%.0fpx²，占画面 %.2f%%）—— 这种点位解出来的单应矩阵"
                         "在点位之外会飞掉：重投影误差照样是 0.00m，"
                         "但篮筐/球员会被投到几十米外。"
                         "请**换几个不在同一条线上**的特征点（例如"
                         "底线两角 + 罚球区两角 + 中圈）。"
                         % (out["src_tri_px2"], 100 * out["src_frac"]))
    elif out["dst_tri_m2"] < max(1.05, min_dst_frac * ref_dst):
        out["degenerate"] = True
        out["reason"] = ("你选的特征点在球场上也几乎共线（展开面积只有 "
                         "%.2fm²）—— 请选**既不同线、又不同侧**的点。"
                         % out["dst_tri_m2"])
    return out


# --------------------------------------------------------------------------
# 单应矩阵（3x3，无 numpy 依赖实现，方便在无第三方库时也能跑）
# --------------------------------------------------------------------------
Matrix3 = list[list[float]]


def _solve_linear(A: list[list[float]], b: list[float]) -> list[float]:
    """高斯消元解 n 元线性方程组（带部分主元）。纯 python，无 numpy。"""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise ValueError("单应矩阵求解失败：点共线或退化，请重新选点")
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(col + 1, n):
            f = M[r][col] / pv
            if f:
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n] - sum(M[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / M[r][r]
    return x


def _normalize(pts: Sequence[Sequence[float]]) -> tuple[list[list[float]],
                                                        list[list[float]]]:
    """Hartley 归一化：平移到质心、缩放到平均距离 √2。返回 (归一化点, 3x3 变换)。"""
    n = len(pts)
    cx = sum(float(p[0]) for p in pts) / n
    cy = sum(float(p[1]) for p in pts) / n
    mean = sum(math.hypot(float(p[0]) - cx, float(p[1]) - cy) for p in pts) / n
    s = (math.sqrt(2.0) / mean) if mean > 1e-12 else 1.0
    T = [[s, 0.0, -s * cx], [0.0, s, -s * cy], [0.0, 0.0, 1.0]]
    out = [[s * (float(p[0]) - cx), s * (float(p[1]) - cy)] for p in pts]
    return out, T


def _mat_mul3(A: Matrix3, B: Matrix3) -> Matrix3:
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def _invert3(M: Matrix3) -> Matrix3:
    det = (M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1])
           - M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0])
           + M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0]))
    if abs(det) < 1e-18:
        raise ValueError("单应矩阵求解失败：点共线或退化，请重新选点")
    return [[(M[1][1] * M[2][2] - M[1][2] * M[2][1]) / det,
             (M[0][2] * M[2][1] - M[0][1] * M[2][2]) / det,
             (M[0][1] * M[1][2] - M[0][2] * M[1][1]) / det],
            [(M[1][2] * M[2][0] - M[1][0] * M[2][2]) / det,
             (M[0][0] * M[2][2] - M[0][2] * M[2][0]) / det,
             (M[0][2] * M[1][0] - M[0][0] * M[1][2]) / det],
            [(M[1][0] * M[2][1] - M[1][1] * M[2][0]) / det,
             (M[0][1] * M[2][0] - M[0][0] * M[2][1]) / det,
             (M[0][0] * M[1][1] - M[0][1] * M[1][0]) / det]]


def find_homography(src: Sequence[Sequence[float]],
                    dst: Sequence[Sequence[float]]) -> Matrix3:
    """求 H 使得 dst ~ H @ src。**4 个点及 4 个以上都支持**。

    为什么必须支持多于 4 个点：界面上写着"同一帧里点 6 个以上、不要都在同一条线上"
    （多点是**唯一**能发现"某个点的名称和位置对不上"的办法），但旧实现是纯方阵
    DLT —— 只有 4 个点时方程才是方的，点 5 个以上直接
    `IndexError: list index out of range`（实测：点 6 个 → 界面报"解算标定失败"）。
    于是"按提示多点几个点"这条路反而必然失败，用户只能退回 4 个点，也就失去了
    自我校验的能力。旧版（HoopAI）用的是最小二乘，n≥4 都能解 —— 这里补回来。

    做法：4 个点保持原来的精确解（行为不变）；多于 4 个点时用
    **归一化坐标 + 最小二乘**（Hartley 归一化，先按质心/尺度归一，
    再解 (AᵀA)h = Aᵀb），这样既是超定最小二乘、数值上也稳
    （不归一化时像素量级 1e3、米量级 1e0，条件数极差）。
    """
    if len(src) < 4 or len(dst) != len(src):
        raise ValueError("至少需要 4 组对应点")
    if len(src) == 4:
        A, b = [], []
        for (x, y), (u, v) in zip(src, dst):
            A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
            b.append(u)
            A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
            b.append(v)
        h = _solve_linear(A, b)
        return [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]]

    s_n, T_src = _normalize(src)
    d_n, T_dst = _normalize(dst)
    A, b = [], []
    for (x, y), (u, v) in zip(s_n, d_n):
        A.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        b.append(u)
        A.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        b.append(v)
    ATA = [[sum(A[r][i] * A[r][j] for r in range(len(A))) for j in range(8)]
           for i in range(8)]
    ATb = [sum(A[r][i] * b[r] for r in range(len(A))) for i in range(8)]
    h = _solve_linear(ATA, ATb)
    Hn = [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]]
    H = _mat_mul3(_invert3(T_dst), _mat_mul3(Hn, T_src))
    if abs(H[2][2]) < 1e-15:
        raise ValueError("单应矩阵求解失败：点共线或退化，请重新选点")
    k = H[2][2]
    return [[v / k for v in H[0]], [v / k for v in H[1]], [v / k for v in H[2]]]


def apply_homography(H: Matrix3, x: float, y: float) -> tuple[float, float]:
    d = H[2][0] * x + H[2][1] * y + H[2][2]
    if abs(d) < 1e-12:
        return 0.0, 0.0
    return ((H[0][0] * x + H[0][1] * y + H[0][2]) / d,
            (H[1][0] * x + H[1][1] * y + H[1][2]) / d)


@dataclass
class Calibration:
    """一次标定结果。可序列化成 calibration.json 存盘/复用。"""
    name: str = "default"
    method: str = "manual"                 # manual | keypoints
    src_px: list[list[float]] = field(default_factory=list)   # 像素点
    dst_m: list[list[float]] = field(default_factory=list)    # 球场米
    H: Optional[Matrix3] = None
    reproj_error_m: float = 0.0
    note: str = ""
    # 目标坐标是「折半坐标」(half) 还是「全场坐标」(full)。
    # keypoints 自动标定可能用全场关键点，所以这个字段显式存下来，
    # 不能靠猜 —— 老文件（没有这个字段）由 _infer_frame 兜底。
    frame: str = "half"
    # 这份标定是**给哪个视频/哪个机位**的。
    # 存在的意义：标定是按机位来的，不是按分辨率来的。以前只比对分辨率，
    # 结果拿公园球场的标定去算 NBA 转播（同样 1280×720）也能通过，
    # 出手点被算到三分线外十几米 —— 看起来像真的，其实全是垃圾。
    # 现在明确绑定视频文件名，对不上就拒绝自动使用。
    for_video: str = ""
    frame_size: list = field(default_factory=list)   # [宽, 高]
    # true 表示点数不足或独立校验未通过；不能据此输出位置类结论。
    position_unverified: bool = False

    def fit(self) -> "Calibration":
        self.H = find_homography(self.src_px, self.dst_m)
        self.reproj_error_m = self.rmse()
        return self

    def rmse(self) -> float:
        if not self.H:
            return 0.0
        errs = []
        for (x, y), (u, v) in zip(self.src_px, self.dst_m):
            px, py = apply_homography(self.H, x, y)
            errs.append(math.hypot(px - u, py - v))
        return round(sum(errs) / len(errs), 4) if errs else 0.0

    def to_court(self, px: float, py: float) -> tuple[float, float]:
        """像素 -> **分析坐标**（米）。这是下游唯一该用的入口。

        ``frame == "full"`` 的标定会先经过 fold_to_analysis 折半；
        半场标定（``frame == "half"``）的目标点本来就是折半坐标，直接返回。
        """
        if not self.H:
            return 0.0, 0.0
        x, y = apply_homography(self.H, px, py)
        return fold_to_analysis(x, y, self.frame or "half")

    def to_pixel(self, x_m: float, y_m: float) -> tuple[float, float]:
        """**分析坐标** -> 像素（画热区叠加、校验标定用）。

        与 to_court 严格互逆：full 标定会先把折半坐标 unfold 回全场坐标
        （fold_to_analysis 自反，所以直接复用同一个函数）。
        """
        inv = invert(self.H) if self.H else None
        if not inv:
            return 0.0, 0.0
        x_m, y_m = unfold_from_analysis(x_m, y_m, self.frame or "half")
        return apply_homography(inv, x_m, y_m)

    @staticmethod
    def _infer_frame(dst_m: list) -> str:
        """老标定文件（没有 frame 字段）的兜底判定：返回 half/left/right/full。

        判据（按可靠性排序）：
          1. y 跨度 >= 27.5m            -> full（真·全场，两条底线在 ±14）
          2. 前两个点的 y >= +13.9      -> right（未折半的右半场，底线在 +14）
          3. 前两个点的 y <= -13.9      -> left （未折半的左半场，底线在 -14）
          4. 其余                        -> half（本来就是折半分析坐标）

        为什么看"前两个点"：四角点的点选顺序固定是
        「底线左 → 底线右 → 中线右 → 中线左」，所以前两个点一定是底线。
        老版本半场标定（底线 -14 / 中线 0）与新版本（底线 0 / 中线 -14）
        y 取值集合一样，只有顺序能区分，这是唯一可靠的信号。
        """
        ys = [p[1] for p in (dst_m or []) if len(p) >= 2]
        if not ys:
            return "half"
        if max(ys) - min(ys) >= 27.5:
            return "full"
        first = ys[:2]
        if first and min(first) >= 13.9:
            return "right"
        if first and max(first) <= -13.9:
            return "left"
        return "half"

    def analysis_bounds(self, tol: float = 0.15
                        ) -> tuple[float, float, float, float]:
        """这份标定在**分析坐标**里覆盖的球场范围 (xmin, xmax, ymin, ymax)。

        为什么要它：半场标定只覆盖半个球场 —— 左半场是 y ∈ [-14,0]，
        右半场是 y ∈ [0,14]。如果下游拿"|y| <= 14"这种对称范围去过滤，
        就会把**远底线后方**（观众/替补席）的点当成场内点收进来
        （实测出现过 y=+7.4 的"球员"停在底线后面）。
        这里直接从标定点反推覆盖范围，什么半场都不会认错。
        """
        if not self.dst_m:
            return (-COURT_WIDTH / 2 - tol, COURT_WIDTH / 2 + tol,
                    -COURT_LENGTH / 2 - tol, COURT_LENGTH / 2 + tol)
        pts = [fold_to_analysis(px, py, self.frame or "half")
               for px, py in self.dst_m]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs) - tol, max(xs) + tol, min(ys) - tol, max(ys) + tol)

    def in_court(self, x: float, y: float, tol: float = 0.15) -> bool:
        """分析坐标是否落在这份标定覆盖的球场内。"""
        x0, x1, y0, y1 = self.analysis_bounds(tol)
        return x0 <= x <= x1 and y0 <= y <= y1

    def save(self, path: str) -> None:
        d = asdict(self)
        d["H"] = self.H
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path: str) -> "Calibration":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        method = str(d.get("method") or "manual")
        src_px = d.get("src_px", [])
        dst_m = d.get("dst_m", [])
        position_unverified = bool(d.get("position_unverified"))
        point_names = d.get("point_names") or []
        # 老版手工标定没有保存校验标志。四点点击没有冗余约束；而早期
        # FIBA 点表把罚球线误放在离中圈 5.8m（正确值是 8.2m）。
        # 对这些存量文件要求重新标定，避免更新程序后继续画出偏移热区。
        if not position_unverified and method == "web-keypoints" and len(src_px) <= 4:
            position_unverified = True
        if not position_unverified and method.startswith(("web-keypoints", "auto-identify")):
            for name, point in zip(point_names, dst_m):
                expected_y = (8.2 if name in ("ft_near", "ft_far") else
                              5.675 if name in ("arc_near", "arc_far") else None)
                if (expected_y is not None and len(point) >= 2
                        and abs(abs(float(point[1])) - expected_y) > 0.05):
                    position_unverified = True
                    break
            # 自动对应版本不存点名；旧表里的罚球线/三分弧顶坐标有误。
            if not point_names and method.startswith("auto-identify"):
                position_unverified = any(
                    len(point) >= 2 and
                    min(abs(abs(float(point[1])) - legacy_y)
                        for legacy_y in (5.8, 6.2, 7.25)) < 0.05
                    for point in dst_m)
        return Calibration(name=d.get("name", "default"),
                           method=method,
                           src_px=src_px, dst_m=dst_m,
                           H=d.get("H"),
                           reproj_error_m=d.get("reproj_error_m", 0.0),
                           note=d.get("note", ""),
                           for_video=d.get("for_video", ""),
                           frame_size=d.get("frame_size", []),
                           position_unverified=position_unverified,
                           frame=d.get("frame") or Calibration._infer_frame(dst_m))

    def matches_video(self, video_path: str, width: int = 0,
                      height: int = 0) -> bool:
        """这份标定是不是给这个视频的。

        判据：`for_video` 记了视频文件名就必须对上；没记（老文件）则退回
        分辨率比对，并且**只在标定点确实落在画面内时**才算通过。
        """
        import os
        base = os.path.basename(str(video_path or "")).lower()
        # 两边都归一化成**文件名**再比（对路径分隔符与大小写不敏感）。
        # 实测踩到的大坑：标定文件里存的是路径（"data\basketball_match.mp4"），
        # 这里却拿它跟 basename（"basketball_match.mp4"）直接比 → 永远不相等
        # → 用户在界面上辛苦标的标定**全部被判成"不适用于这段视频"**，
        # 热区/战术图一个都不生成，界面只显示一句"机位/分辨率不匹配"，
        # 用户完全无从判断（标定明明存下来了、误差还只有 0.693m）。
        if self.for_video:
            fv = os.path.basename(str(self.for_video).replace("\\", "/")).lower()
            return bool(base) and bool(fv) and fv == base
        src = self.src_px or []
        if not src or not width or not height:
            return False
        return (max(p[0] for p in src) <= width * 1.05
                and max(p[1] for p in src) <= height * 1.05)


def invert(H: Matrix3) -> Optional[Matrix3]:
    """3x3 求逆（伴随矩阵法）。"""
    a, b, c = H[0]
    d, e, f = H[1]
    g, h, i = H[2]
    A = e * i - f * h
    B = -(d * i - f * g)
    C = d * h - e * g
    det = a * A + b * B + c * C
    if abs(det) < 1e-12:
        return None
    return [[A / det, -(b * i - c * h) / det, (b * f - c * e) / det],
            [B / det, (a * i - c * g) / det, -(a * f - c * d) / det],
            [C / det, -(a * h - b * g) / det, (a * e - b * d) / det]]


# --------------------------------------------------------------------------
# 预置标定点：让用户点 4 个角点即可
# --------------------------------------------------------------------------
# 全场 4 个角点（**未折半**的全场坐标，米）：两条底线在 y=∓14、中线在 y=0。
# 用这套点算出来的标定 frame="full"，下游必须先 fold_to_analysis() 折半。
FULL_COURT_CORNERS = [
    (-COURT_WIDTH / 2, -COURT_LENGTH / 2),   # 左下
    (COURT_WIDTH / 2, -COURT_LENGTH / 2),    # 右下
    (COURT_WIDTH / 2, COURT_LENGTH / 2),     # 右上
    (-COURT_WIDTH / 2, COURT_LENGTH / 2),    # 左上
]

# 半场 4 个角点（只拍到半场时的推荐配置），**直接用折半坐标**：
#   底线在 |y|=0、中线在 |y|=14，篮筐 (0,±1.575) 正好距底线 1.575m。
# 约定这支摄像机拍的是左半场（y<0），任务列表里 home_hoop 默认也是 left。
#
# ⚠️ 这里曾经是一个**真实的 bug**：老版本把底线映射到 y=-14、中线映射到 y=0，
#    于是篮下上篮被算成离篮筐 12.4m，规则引擎判成三分；俯视战术图也会整体
#    偏移 14m。现在 HALF_COURT_CORNERS / fold_to_analysis / 前端 court.js
#    的 v=|y|（v=0 是底线）三者严格一致，并由
#    tests/test_plan_b.py::test_calibration_layup_is_two_points 守住。
HALF_COURT_CORNERS = [
    (-COURT_WIDTH / 2, 0.0),                   # 底线左
    (COURT_WIDTH / 2, 0.0),                    # 底线右
    (COURT_WIDTH / 2, -COURT_LENGTH / 2),      # 中线右
    (-COURT_WIDTH / 2, -COURT_LENGTH / 2),     # 中线左
]


def calibrate_from_corners(pixel_corners: Sequence[Sequence[float]],
                           half_court: bool = False,
                           name: str = "court",
                           video_path: str = "",
                           frame_size: Optional[Sequence[int]] = None
                           ) -> Calibration:
    """最简单可靠的标定：用户按顺序点 4 个角点。

    顺序：底线左 -> 底线右 -> 中线右 -> 中线左（或全场四角逆/顺时针）。
    """
    import os
    dst = HALF_COURT_CORNERS if half_court else FULL_COURT_CORNERS
    c = Calibration(name=name, method="manual",
                    src_px=[list(p) for p in pixel_corners],
                    dst_m=[list(p) for p in dst],
                    frame="half" if half_court else "full",
                    note="四角点手动画定" + ("（半场）" if half_court else "（全场）"),
                    for_video=os.path.basename(str(video_path)) if video_path else "",
                    frame_size=[int(v) for v in (frame_size or [])])
    return c.fit()


def calibrate_from_keypoints(kp_px: dict[str, Sequence[float]],
                             model_dst: Optional[dict[str, Sequence[float]]] = None
                             ) -> Calibration:
    """用球场关键点检测模型的输出标定（B 套餐的升级路径）。

    kp_px: {"left_baseline_corner_top": (x,y), ...} 至少 4 个，
           名称需与 model_dst 的键一致。
    默认提供一套常见关键点的球场真实坐标（半场进攻右侧篮筐）。
    """
    default_dst = {
        "baseline_left":  (-COURT_WIDTH / 2, -COURT_LENGTH / 2),
        "baseline_right": (COURT_WIDTH / 2, -COURT_LENGTH / 2),
        "half_left":      (-COURT_WIDTH / 2, 0.0),
        "half_right":     (COURT_WIDTH / 2, 0.0),
        "hoop":           (1.575, -COURT_LENGTH / 2 + 1.575),
        "center":         (0.0, 0.0),
    }
    model_dst = model_dst or default_dst
    src, dst = [], []
    for k, px in kp_px.items():
        if k in model_dst:
            src.append(list(px))
            dst.append(list(model_dst[k]))
    if len(src) < 4:
        raise ValueError(f"可用关键点不足 4 个（当前 {len(src)}）")
    c = Calibration(name="keypoints", method="keypoints",
                    src_px=src, dst_m=dst, note="关键点模型自动标定",
                    frame=Calibration._infer_frame(dst))
    return c.fit()
