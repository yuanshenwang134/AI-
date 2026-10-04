"""导出模块：CSV / Excel 可打开格式 / 结构化 JSON。

用标准库 csv，加 UTF-8 BOM 以便 Excel 双击直接正确显示中文。
"""
from __future__ import annotations

import csv
import io
from typing import Optional

from .model import Shot
from .rules import TeamStats, zone_of


def write_stats_csv(path: str, players: list[dict],
                    home: Optional[TeamStats] = None,
                    away: Optional[TeamStats] = None) -> None:
    cols = ["player_id", "name", "jersey", "team", "points",
            "fgm", "fga", "fg_pct", "tpm", "tpa", "tp_pct",
            "ftm", "fta", "ft_pct", "efg", "ts",
            "reb", "ast", "stl", "blk", "tov", "pf"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["球员统计表"])
        w.writerow(cols)
        for p in players:
            w.writerow([p.get(c, "") for c in cols])
        if home and away:
            w.writerow([])
            w.writerow(["球队汇总"])
            w.writerow(["team", "points", "fgm", "fga", "fg_pct",
                        "tpm", "tpa", "tp_pct", "ftm", "fta"])
            for t in (home, away):
                d = t.to_dict()
                w.writerow([d.get(c, "") for c in
                            ["team", "points", "fgm", "fga", "fg_pct",
                             "tpm", "tpa", "tp_pct", "ftm", "fta"]])


def write_shots_csv(path: str, shots: list[Shot]) -> None:
    cols = ["t", "period", "team", "player_id", "value", "made", "points",
            "counts_for_score", "x", "y", "distance", "zone",
<<<<<<< HEAD
            "outcome_source", "confidence", "tags", "result"]
=======
            "outcome_source", "confidence", "tags"]
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in sorted(shots, key=lambda x: x.t):
<<<<<<< HEAD
            # 位置未知（占位坐标）的出手：距离导出为空、区域写"位置未知"。
            # 不能照抄 x/y 算出来的 0.0 米/禁区 —— 那是"所有出手都在篮下"的假数据，
            # 与界面/战报里"位置未知、不给热区"的说法自相矛盾。
            known = s.location_known
            w.writerow([round(s.t, 2), s.period, s.team, s.player_id, s.value,
                        ("" if s.result == "unknown" else int(s.made)), s.score_points, int(s.counts_for_score),
                        round(s.x, 3), round(s.y, 3),
                        (round(s.distance, 3) if known else ""),
                        (zone_of(s.x, s.y) if known else "位置未知"),
                        s.outcome_source, s.confidence, "|".join(s.tags), s.result])
=======
            w.writerow([round(s.t, 2), s.period, s.team, s.player_id, s.value,
                        int(s.made), s.score_points, int(s.counts_for_score),
                        round(s.x, 3), round(s.y, 3),
                        round(s.distance, 3), zone_of(s.x, s.y),
                        s.outcome_source, s.confidence, "|".join(s.tags)])
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f


def shots_to_csv_string(shots: list[Shot]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["t", "period", "team", "player_id", "value", "made", "points",
<<<<<<< HEAD
                "x", "y", "zone", "result"])
    for s in sorted(shots, key=lambda x: x.t):
        w.writerow([round(s.t, 2), s.period, s.team, s.player_id, s.value,
                    ("" if s.result == "unknown" else int(s.made)), s.score_points, round(s.x, 3), round(s.y, 3),
                    (zone_of(s.x, s.y) if s.location_known else "位置未知"), s.result])
=======
                "x", "y", "zone"])
    for s in sorted(shots, key=lambda x: x.t):
        w.writerow([round(s.t, 2), s.period, s.team, s.player_id, s.value,
                    int(s.made), s.score_points, round(s.x, 3), round(s.y, 3),
                    zone_of(s.x, s.y)])
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    return buf.getvalue()


# --------------------------------------------------------------------------
# 套餐 B：战术层导出
# --------------------------------------------------------------------------
def write_passes_csv(path: str, tactics: dict) -> None:
    """传球 / 失误明细 + 传球网络汇总。

    三个区块写进同一个 CSV（Excel 双击可看），和 stats.csv 的写法保持一致：
      ① 传球事件  逐次传球（谁传给谁、距离、球权是否转移）
      ② 传球网络  球员之间的传球次数（有向）
      ③ 球员传球  每名球员的传球 / 接球数
    """
    passes = (tactics or {}).get("passes") or {}
    events = passes.get("events") or []
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["传球 / 失误事件"])
        w.writerow(["t", "kind", "team", "from", "to", "dist_m",
                    "battle_s", "x", "depth"])
        for e in events:
            w.writerow([e.get("t"), e.get("kind", "pass"), e.get("team"),
                        e.get("from"), e.get("to"), e.get("dist"),
                        e.get("duration"), e.get("x"), e.get("d")])

        w.writerow([])
        w.writerow(["传球网络（有向：从 -> 到）"])
        w.writerow(["team", "from", "to", "count"])
        net = (tactics or {}).get("pass_network") or {}
        for team, g in sorted(net.items()):
            for e in (g.get("edges") or []):
                w.writerow([team, e.get("source"), e.get("target"),
                            e.get("value")])

        w.writerow([])
        w.writerow(["球员传球统计"])
        w.writerow(["player_id", "name", "team", "passes", "received"])
        for r in (passes.get("leaderboard") or []):
            w.writerow([r.get("player_id"), r.get("name"), r.get("team"),
                        r.get("passes"), r.get("received")])


def write_spacing_csv(path: str, tactics: dict) -> None:
    """空间指标时间序列（前端"间距曲线"的数据源，答辩时可直接翻表核对）。"""
    rows = ((tactics or {}).get("spacing") or {}).get("timeline") or []
    cols = ["t", "team", "n", "area", "mean_dist", "width", "depth",
            "radius", "cx", "cd"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["球队空间指标（时间序列）",
                    "area=凸包面积m²  mean_dist=平均间距m  depth=纵深m  "
                    "radius=重心到篮筐m"])
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
