"""把「篮下视觉判进球」（`hoopsight`）的结论接进计分链路。

拆成独立小模块的原因：
  * `sources.py` 已经很长，而这块逻辑是**纯几何/统计**的（不碰视频），
    单独放在这里才能被单元测试直接喂数据断言；
  * 它要回答两个问题，而且都要能说清「依据是什么」：

  1. 这一球是哪一队得的？（`attribute_teams`）
     主证据：穿筐那一刻**离篮筐最近的球员属于哪队**（上篮/补篮的人就在筐下）。
     兜底：全场离筐最近的一批球员的球衣颜色投票。
     两条都不行就**如实留空**（计分时按 `fallback_team` 归主队并标注不明），
     绝不硬猜一个然后当事实写进战报。

  2. 这一球值几分？（`value_of`）
     有可用球场标定时：按「出手点距篮筐的距离 + 是否踩线」判 2/3 分，
     和规则引擎同一套 FIBA 几何（`rules.is_three_pointer`）。
     没标定时：按 `default_value` 记分并标 `value_assumed=True`，
     因为单目图像距离换不成米 —— 这条限制在 README 里写明。
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def _players_near_rim_px(player_obs: Sequence[dict], t: float, hoop_px,
                         max_dt: float = 0.8, max_dist_px: float = 700.0,
                         k: int = 3) -> list[dict]:
    """穿筐时刻离篮筐最近的 k 个球员（**画面像素坐标**），含距离。

    为什么用像素而不是球场坐标：判进球发生在篮筐那一小块区域，像素距离是
    直接测量值；而球场坐标要先过单应矩阵，标定一坏（实测把画面里的篮筐投到
    离真篮筐 8.3m 处）就会把离筐 1m 的人算到 8m 外，进球直接记到别队头上。

    `max_dist_px` 给得比较宽（跑篮下的人可能没被检出，退而取场上最近的那个），
    但调用方会用 `max_conclusive_px` 判断这个距离**够不够格当证据** ——
    不够就标 low，绝不把「场上最近的人」当成「投进的人」。
    """
    if not player_obs or hoop_px is None:
        return []
    hx, hy, hr = float(hoop_px[0]), float(hoop_px[1]), float(hoop_px[2] or 1.0)
    lim = max(float(max_dist_px), hr * 4.5)
    out = []
    for o in player_obs:
        if abs(float(o.get("t", -9)) - t) > max_dt:
            continue
        d = math.hypot(float(o.get("foot_x", 0)) - hx,
                       float(o.get("foot_y", 0)) - hy)
        if d > lim:
            continue
        out.append(dict(o, dist_px=round(d, 1)))
    out.sort(key=lambda o: o["dist_px"])
    return out[:max(1, k)]


def filter_shots_by_labels(shots: Sequence, labels: Sequence[dict],
                           tol: float = 1.2) -> tuple[list, list]:
    """人工标注白名单：只保留「标注为进球」的时刻附近的自动判定。

    返回 (保留的 shot, 丢掉的时间列表)。

    为什么需要这个兜底：实测有些机位上自动判据 precision = 0%
    （nybo_3min：3 个自动判定全部是误报，人工确认整段 0 进球）。
    那种情况下继续输出自动结果就是错的比分；把人工标注当准绳，
    系统至少是"诚实且正确"的，也能把误报作为**已知失败案例**记在案。
    """
    made = [(float(r.get("t0", 0.0)), float(r.get("t1", r.get("t0", 0.0))))
            for r in labels if str(r.get("label")) == "made"]
    keep, drop = [], []
    for s in shots:
        t = float(getattr(s, "t", 0.0))
        if any(t0 - tol <= t <= t1 + tol for t0, t1 in made):
            keep.append(s)
        else:
            drop.append(s)
    return keep, drop


def _nearest_player(player_obs: Sequence[dict], t: float, hoop_px,
                    max_dt: float = 0.8):
    """向后兼容：旧调用方传的是球场坐标采样，这里统一走像素观测。"""
    got = _players_near_rim_px(player_obs, t, hoop_px, max_dt=max_dt, k=1)
    return got[0] if got else None


def _closest_players(player_track: Sequence, hoop_px, k: int = 6,
                     window: float = 4.0):
    """（保留）整段视频里离篮筐最近的 k 个球场坐标采样 —— 仅在退化路径用它。"""
    if not player_track or hoop_px is None:
        return []
    hx, hy, _hr = hoop_px
    scored = []
    for s in player_track:
        scored.append((math.hypot(s.x - hx, s.y - hy), s))
    scored.sort(key=lambda kv: kv[0])
    return [s for _d, s in scored[:max(1, k)]]


# 默认「向前找」多少秒。这个数字是**在 basketball_match.mp4 上对着比分牌
# 真值标定出来的**（8 次得分事件）：
#   进球瞬间(t-0.5s)  → 5/8 判对（62%）
#   进球前 1.5s        → 6/8 判对（75%）  ← 最好
#   进球前 3.0s        → 5/8 判对（62%）
# 为什么"瞬时"反而差：得分那一下篮筐下面站的多半是**防守人/抢篮板的人**，
# 投进的人已经落地/转身了。往前看一点，拿球的还是进攻方，所以更准。
DEFAULT_LOOKBACK_S = 1.5

# 最近球员离筐超过这个距离就**不判**（标未定）。
# 门槛是在 basketball_match.mp4 的 8 次事件上量出来的（`out/tmp/threshold_effect.py`）：
#     门槛   判对  判错  未定   有效准确率
#     1.5m    4     0     4      100%
#     1.7m    6     1     1       86%
#     2.0m    6     2     0       75%   ← 不设门槛的结果
# 取 1.5m：**凡是我们敢判的，全对**；代价是 4/8 标成未定。
# 这符合本工程的取向 —— 宁可说"这球我判不出，请人工定"，也不给一个错的队别。
MAX_LOOKBACK_DIST_M = 1.5


def attribute_by_pixel_lookback(shots: Sequence, player_obs: Sequence[dict],
                                hoop_px, lookback_s: float = DEFAULT_LOOKBACK_S,
                                k: int = 5, win: float = 1.0,
                                max_dist_rx: float = 2.2) -> dict:
    """**像素空间**的「向前找」归因 —— 不经过球场标定，所以标定坏了也不受影响。

    为什么需要这一条：实测两份标定都是坏的（把画面里的篮筐投到离真篮筐
    8~12m 外），用它们的球员坐标去判队等于拿随机数判。而球员的**像素脚底点**
    （`RawTrack.player_obs`）是直接观测值，永远可信。

    判据与 `attribute_by_lookback` 相同（进球前 `lookback_s` 秒、离筐最近的
    `k` 人投票），只是量距离用像素：
      * 门槛按**篮圈半宽**给（`max_dist_rx × rx`），因为像素距离没有绝对尺度；
      * 够不着就标未定，不猜。
    """
    out: dict[int, dict] = {}
    if not player_obs or hoop_px is None:
        return out
    hx, hy = float(hoop_px[0]), float(hoop_px[1])
    rx = float(hoop_px[2] or 40.0)
    lim = rx * float(max_dist_rx)

    for i, sh in enumerate(shots):
        t = float(getattr(sh, "t", 0.0))
        tt = max(0.0, t - float(lookback_s))
        near = []
        for o in player_obs:
            if abs(float(o.get("t", -9.0)) - tt) > win:
                continue
            d = math.hypot(float(o.get("foot_x", 0.0)) - hx,
                           float(o.get("foot_y", 0.0)) - hy)
            near.append((d, o))
        near.sort(key=lambda kv: kv[0])
        near = near[:max(1, k)]
        tally = {"home": 0, "away": 0}
        for _d, o in near:
            tm = str(o.get("team", ""))
            if tm in tally:
                tally[tm] += 1
        d_min = round(near[0][0], 1) if near else 999.0
        if not (tally["home"] or tally["away"]):
            continue
        if d_min > lim:
            team, conf = "", "unknown"
            why = (f"像素向前找：进球前 {lookback_s:.1f}s 离筐最近的球员也在 "
                   f"{d_min:.0f}px（>{lim:.0f}px ≈ {lim / 90:.1f}m）外 → "
                   "筐边没人可判，标未定")
        elif tally["home"] == tally["away"]:
            team, conf = "", "unknown"
            why = f"像素向前找：最近 {len(near)} 人两队各半（{tally}）→ 未定"
        else:
            team = "home" if tally["home"] > tally["away"] else "away"
            conf = "low" if min(tally.values()) else "ok"
            why = (f"像素向前找：进球前 {lookback_s:.1f}s（t={tt:.2f}s）离筐最近的 "
                   f"{len(near)} 人（最近 {d_min:.0f}px）里 {team} 占 "
                   f"{tally[team]}/{len(near)} → {team}")
        out[i] = {"team": team, "reason": why, "confidence": conf,
                  "vote": dict(tally), "d_min_px": d_min,
                  "lookback_s": lookback_s,
                  "source": "pixel_obs",
                  "nearest": [{"player_id": o.get("player_id"),
                               "dist_px": round(d, 1),
                               "team": o.get("team"), "t": o.get("t")}
                              for d, o in near]}
    return out


def attribute_by_lookback(shots: Sequence, players: dict,
                          player_track: Sequence, hoop_px,
                          lookback_s: float = DEFAULT_LOOKBACK_S,
                          k: int = 5, win: float = 1.0,
                          max_dist_m: float = MAX_LOOKBACK_DIST_M) -> dict:
    """「向前找」版归因：看进球**前** `lookback_s` 秒，谁离被进攻篮筐最近。

    这是用户在 `basketball_match.mp4` 上提出的思路，也是被真值验证过更准的那条
    （见 `DEFAULT_LOOKBACK_S` 的注释）。判据：
      1. 取该时刻离筐最近的 `k` 名球员，哪一队占多数就判哪队；
      2. **但如果最近的球员都太远（> `max_dist_m`），就标未定** ——
         实测两个判错的样本都是「最近的球员是防守方、且离筐 1.9m / 0.9m」，
         这种球拿"最近的人"当答案本来就是错的，宁可说不知道。

    返回 {shot_index: {...}}，判不出就留空 —— 与 `attribute_teams` 同一套约定。
    """
    out: dict[int, dict] = {}
    if not player_track or hoop_px is None:
        return out
    hx, hy, _hr = hoop_px

    for i, sh in enumerate(shots):
        t = float(getattr(sh, "t", 0.0))
        tt = max(0.0, t - float(lookback_s))
        near = []
        for s in player_track:
            if abs(float(getattr(s, "t", 0.0)) - tt) > win:
                continue
            near.append((math.hypot(s.x - hx, s.y - hy), s))
        near.sort(key=lambda kv: kv[0])
        near = near[:max(1, k)]
        tally = {"home": 0, "away": 0}
        for _d, s in near:
            p = players.get(getattr(s, "player_id", ""))
            if p is not None and getattr(p, "team", "") in tally:
                tally[p.team] += 1
        d_min = round(float(near[0][0]), 2) if near else 99.0
        conf_mark = "ok"

        if not (tally["home"] or tally["away"]):
            continue
        if d_min > max_dist_m:
            # 最近的人都不在筐边 —— 多半这一球是从外线投的/球轨迹断了，
            # 没有"最后持球人"可言。标未定，不猜。
            team = ""
            conf_mark = "unknown"
            why = (f"向前找：进球前 {lookback_s:.1f}s（t={tt:.2f}s）离筐最近的"
                   f"球员也在 {d_min:.1f}m 外（>{max_dist_m:.1f}m）→ "
                   "没有可信的最后持球人，判不出")
        elif tally["home"] == tally["away"]:
            team, conf_mark = "", "unknown"
            why = (f"进球前 {lookback_s:.1f}s 离筐最近的人两队各半"
                   f"（{tally}）→ 判不出")
        else:
            team = "home" if tally["home"] > tally["away"] else "away"
            conf_mark = "low" if min(tally.values()) else "ok"
            why = (f"向前找：进球前 {lookback_s:.1f}s（t={tt:.2f}s）离筐最近的 "
                   f"{len(near)} 人里 {team} 占 {tally[team]}/{len(near)}"
                   f"（最近 {d_min:.2f}m）→ {team}")
        out[i] = {"team": team, "reason": why, "confidence": conf_mark,
                  "nearest": [{"player_id": getattr(s, "player_id", ""),
                               "dist_m": round(math.hypot(s.x - hx, s.y - hy), 2),
                               "t": getattr(s, "t", 0.0)} for _d, s in near],
                  "vote": dict(tally), "lookback_s": lookback_s,
                  "d_min": d_min}
    return out


def _matches_team_colour(colour_name: str, jersey_colors: Optional[dict]):
    """球衣色标签 → 队别；认不出返回 None。

    只做**保守**的映射：主队/客队的标签（如「黄」「白色/浅灰」）必须在语义上
    属于同一个色系才认。浅色系的标签互相兼容（白/灰/浅灰），因为那本来就是
    同一件球衣在不同光照下的读数。
    """
    if not colour_name or not isinstance(jersey_colors, dict):
        return None
    legend = jersey_colors.get("legend") or {}
    if not legend:
        return None

    def bucket(name: str) -> str:
        n = str(name or "")
        if any(k in n for k in ("白", "灰")):
            return "light"
        if "黄" in n:
            return "yellow"
        if "蓝" in n:
            return "blue"
        if "红" in n:
            return "red"
        if "绿" in n:
            return "green"
        if "黑" in n or "深色" in n:
            return "dark"
        return n

    want = bucket(colour_name)
    hits = [t for t in ("home", "away")
            if bucket(legend.get(t, "")) == want]
    if len(hits) == 1:
        return hits[0]
    return None


def attribute_teams(shots: Sequence, players: dict,
                    player_track: Sequence, hoop_px,
                    fallback_team: str = "home",
                    player_obs: Optional[Sequence[dict]] = None,
                    min_dist_margin: float = 1.6,
                    max_conclusive_px: float = 300.0,
                    motion_blobs: Optional[Sequence[dict]] = None,
                    jersey_colors: Optional[dict] = None,
                    motion_window_s: float = 2.5,
                    motion_max_dist_rx: float = 3.0) -> dict:
    """给每次进球定球队，并把**判据**一起带出来（可复核）。

    判据按可靠度分四档，代码只用「确实成立」的那一档：

      A. `player_obs`（YOLO 球员框，像素坐标）里**筐下**最近的人
         —— 上篮/补篮的人就在筐下，这条最硬；
      B. `motion_blobs`（篮筐窗口里的**人形运动块**，见 `hoopsight._person_blobs`）
         里离筐最近、且在进球前 `motion_window_s` 内出现的那个：
         它的球衣主色与 `jersey_colors` 的 legend 对上 → 定队；
      C. 整段「近筐球员采样」的球衣颜色投票（很粗）；
      D. 以上都不成立 → **留空（未定）**，只把证据报出来。

    为什么 B 是单独一档：实测有些素材里 YOLO 全场没在篮筐附近检出过人
    （小球员个头小 + 底线遮挡），但篮筐窗口里的**运动块**能测到 ——
    这时用「块的上半部颜色 vs 两队球衣色」就能定队，比拿场上随便一个人硬凑强。
    """
    by_shot: dict[int, dict] = {}
    n_nearest = 0
    n_motion = 0
    n_vote = 0
    n_unknown = 0

    # 兜底用的颜色投票（像素观测缺失时才有意义）
    vote_team = None
    vote_detail = {}
    tally = {"home": 0, "away": 0}
    for s in _closest_players(player_track, hoop_px, k=8):
        p = players.get(getattr(s, "player_id", ""))
        if p is not None and getattr(p, "team", "") in tally:
            tally[p.team] += 1
    if tally["home"] or tally["away"]:
        vote_detail = dict(tally)
        if tally["home"] != tally["away"]:
            vote_team = "home" if tally["home"] > tally["away"] else "away"

    for i, sh in enumerate(shots):
        hoop = (sh.hoop_cx, sh.hoop_cy, sh.hoop_rx)
        team = ""
        why = ""
        conf = "unknown"
        player_id = ""
        near_two = []
        if player_obs:
            near_two = _players_near_rim_px(player_obs, getattr(sh, "t", 0.0),
                                            hoop, k=3)
        if near_two:
            first = near_two[0]
            player_id = str(first.get("player_id", ""))
            t_first = first.get("team", "")
            t_second = near_two[1].get("team", "") if len(near_two) > 1 else ""
            d1 = float(first.get("dist_px", 0.0))
            d2 = (float(near_two[1].get("dist_px", 9e9))
                  if len(near_two) > 1 else 9e9)
            if d1 > max_conclusive_px:
                # 最近的人都不在筐下 —— 不能拿他当进球的人
                conf = "unknown"
                why = (f"筐下没有检出的球员（全局离筐最近的一次也有 "
                       f"{d1:.0f}px ≈ {d1 / 90.0:.1f}m），无法判定是哪一队进的；"
                       f"证据：t={sh.t:.2f}s 时场上最近的是 {player_id}"
                       f"（{t_first}，{d1:.0f}px）")
            elif t_first in ("home", "away"):
                team = t_first
                n_nearest += 1
                if t_second and t_second != t_first and d2 < d1 * min_dist_margin:
                    conf = "low"
                    why = (f"穿筐瞬间筐下最近的是 {player_id}（{t_first}，"
                           f"{d1:.0f}px），但 {near_two[1].get('player_id')}"
                           f"（{t_second}，{d2:.0f}px）几乎一样近 —— 分不清，"
                           "按最近的记，标低置信")
                else:
                    conf = "ok"
                    why = (f"穿筐瞬间离筐最近的是 {player_id}（{t_first}，"
                           f"脚底离筐心 {d1:.0f}px；次近 {d2:.0f}px）")

        # ---- B 档：篮筐窗口里的「人形运动块」+ 球衣色 -----
        if not team and motion_blobs:
            cands = [p for p in motion_blobs
                     if -motion_window_s <= float(p.get("t", -9)) - sh.t
                     <= 0.6]
            lim = float(hoop[2]) * motion_max_dist_rx
            cands = [p for p in cands if float(p.get("dist_px", 9e9)) <= lim]
            cands.sort(key=lambda p: (float(p.get("dist_px", 9e9)),
                                      abs(float(p.get("t", 0)) - sh.t)))
            for p in cands[:3]:
                got = _matches_team_colour(p.get("colour_name", ""), jersey_colors)
                if got:
                    team = got
                    conf = "low"
                    dt = sh.t - float(p.get("t", sh.t))
                    why = ((why + "；" if why else "")
                           + f"改用筐下的人形运动块（t={p.get('t'):.2f}s，"
                             f"离筐 {p.get('dist_px'):.0f}px，球衣色 "
                             f"{p.get('colour_name')}）→ {got}")
                    n_motion += 1
                    break
            if not team and cands:
                p = cands[0]
                why = ((why + "；" if why else "")
                       + f"筐下有运动块（t={p.get('t'):.2f}s，"
                         f"{p.get('colour_name')}，离筐 {p.get('dist_px'):.0f}px）"
                         "但颜色与两队球衣都对不上，仍判不出")

        if not team and vote_team and not near_two:
            team = vote_team
            conf = "low"
            why = f"没有可用的近筐球员观测，退回球衣颜色投票 → {team}（{vote_detail}）"
            n_vote += 1
        if not team:
            team = ""
            conf = "unknown"
            if not why:
                why = "判不出（没有近筐球员观测，也没有颜色投票）→ 记未定"
            n_unknown += 1
        by_shot[i] = {
            "team": team, "reason": why, "confidence": conf,
            "player_id": player_id,
            "nearest": [{"player_id": o.get("player_id"), "team": o.get("team"),
                         "dist_px": o.get("dist_px"), "t": o.get("t")}
                        for o in near_two],
        }

    return {"by_shot": by_shot, "meta": {
        "nearest": n_nearest, "motion_blob": n_motion, "color_vote": n_vote,
        "unknown": n_unknown, "vote_detail": vote_detail,
        "max_conclusive_px": max_conclusive_px,
        "method": ("nearest-player-px" if player_obs else "color-vote")}}


def value_of(cal, x: float, y: float, is_free_throw: bool = False,
             default_value: int = 2) -> tuple[int, str]:
    """这一次进球值几分 → (分值, 来源)。

    有标定（x/y 是球场坐标）时按 FIBA 几何判；没有就是默认值 + 假设标记。
    """
    if is_free_throw:
        return 1, "free_throw"
    if cal is None:
        return int(default_value), "visual_estimate"
    try:
        from .rules import is_three_pointer
        return (3 if is_three_pointer(x, y) else 2), "visual_estimate"
    except Exception:
        return int(default_value), "visual_estimate"


<<<<<<< HEAD
def calibration_hoop_error_m(cal, hoop_px) -> Optional[float]:
    """画面里的篮筐经这份标定投到地面后，离真篮筐 (0, ±1.575) 多少米。

    返回 None 表示没有可用标定或投影失败。**单独给出这个数值**是为了让"保存标定"
    能分级：投偏 4m 和投偏 90m 不是一回事 —— 前者还可以存下来让人去修（位置结论
    一律标注未校验），后者是彻底错的东西，没有必要存。以前只有 True/False，
    于是"误差 3.1m"与"误差 90m"被同样对待（都直接拒绝保存）。
    """
    if cal is None or not getattr(cal, "H", None) or hoop_px is None:
        return None
    try:
        hx, hy = cal.to_court(float(hoop_px[0]), float(hoop_px[1]))
    except Exception:  # noqa: BLE001
        return None
    if not (math.isfinite(hx) and math.isfinite(hy)):
        return None
    return min(math.hypot(hx, hy - 1.575), math.hypot(hx, hy + 1.575))


=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
def calibration_sane_for_scoring(cal, hoop_px,
                                 max_dist_m: float = 3.0) -> tuple[bool, str]:
    """这份标定能不能用来判「这个球值 2 分还是 3 分」？

    判据：画面里检测到的篮筐，用标定投到地面后必须落在真实篮筐
    (0, ±1.575) 附近。不满足就说明标定不是这个机位/这个视角的
    —— 实测踩过两次：
      * 一份 4 角点手画的标定把画面里的篮筐投到了 (-5.0, 8.2)，离真篮筐 8.2m；
      * 另一份（basketball_match）更离谱：反算 (0,±1.575) 得到像素 (639,672)
        与 (641,760)，而那段素材里篮筐明明在画面**上方的另一端**（≈(900,160)）
        —— 这两个像素落在比分牌覆盖层上。用它算出来的球员坐标全是垃圾
        （"离篮筐最近的球员"其实离筐 20m 外）。
    所以：**标定必须先过这一关，否则球员位置一律不能用来判队/判分值。**
    """
    if cal is None or not getattr(cal, "H", None) or hoop_px is None:
        return False, "没有可用标定"
    try:
        hx, hy = cal.to_court(float(hoop_px[0]), float(hoop_px[1]))
    except Exception as e:  # noqa: BLE001
        return False, f"标定投影失败：{type(e).__name__}: {e}"
<<<<<<< HEAD
    d = calibration_hoop_error_m(cal, hoop_px)
    if d is None:
        return False, "标定投影失败：算不出篮筐落点"
=======
    d = min(math.hypot(hx, hy - 1.575), math.hypot(hx, hy + 1.575))
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    if d > max_dist_m:
        return False, (f"标定把画面里的篮筐投到了 ({hx:.1f}, {hy:.1f})，"
                       f"离真实篮筐 {d:.1f}m —— 这份标定不属于这个视角，"
                       "不能用来判 2/3 分，也不能用它的球员坐标判队")
    return True, f"篮筐投影落在真实篮筐 {d:.2f}m 内"


def hoopsight_to_attempts(scan, rt, cal=None, fps: float = 30.0,
                          default_value: int = 2,
                          hometown: str = "home",
                          counts_for_score: bool = True,
                          hoop_side: Optional[dict] = None,
                          cal_ok: Optional[bool] = None,
                          team_override: Optional[dict] = None,
                          jersey_colors: Optional[dict] = None,
                          lookback_s: float = DEFAULT_LOOKBACK_S) -> tuple[list, dict]:
    """`hoopsight.SightScan` → (Attempt 列表, 分队诊断)。

    * 分队：见 `attribute_teams` —— 主判据是「穿筐瞬间筐下最近的球员」，
      用的是**画面像素坐标**（标定坏了也不受影响）；判不出就如实留空。
      `team_override` = {进球序号(1 起): "home"/"away"} 是人工指定，优先级最高。
    * 位置：**只有在标定可信时**才把穿筐点投到地面。
      注意球穿筐时是**在空中**，穿筐点投到地面会有系统偏差；这里用它只是为了
      判 2/3 分（远近），并把 `location_estimated` 标上，绝不当作测量值。
      标定不可信就退回被进攻篮筐的占位坐标，分值按 `default_value` 记并标注。
    """
    from .sources import Attempt

    side = hoop_side or {"home": "left", "away": "right"}
    shots = list(getattr(scan, "shots", []) or [])
    if not shots:
        return [], {"method": "none", "reason": "没有判到进球"}

    hoop_px = None
    if getattr(scan, "hoops", None):
        h0 = scan.hoops[0]
        hoop_px = (h0.get("cx", 0.0), h0.get("cy", 0.0), h0.get("rx", 1.0))

    att = attribute_teams(shots, getattr(rt, "players", {}) or {},
                          getattr(rt, "player_track", []) or [], hoop_px,
                          fallback_team=hometown,
                          player_obs=getattr(rt, "player_obs", None) or None,
                          motion_blobs=getattr(scan, "people", None) or None,
                          jersey_colors=jersey_colors)
    # 人工指定优先（序号从 1 起，按时间排序）
    applied = {}
    for idx, team in (team_override or {}).items():
        try:
            i = int(idx) - 1
        except Exception:
            continue
        if 0 <= i < len(shots):
            applied[i] = team
    if applied:
        for i, team in applied.items():
            info = att["by_shot"].setdefault(i, {})
            info["team"] = team
            info["confidence"] = "manual"
            info["reason"] = f"人工指定为 {team}（--basket-teams）"
        att["meta"]["manual_override"] = {str(k + 1): v
                                         for k, v in applied.items()}

    for i, info in (att.get("by_shot") or {}).items():
        if i < len(shots):
            # 把判据挂到进球事件上，报告里能逐球讲清「凭什么说是这队」
            try:
                shots[i].team_reason = info.get("reason", "")
                shots[i].team_confidence = info.get("confidence", "")
            except Exception:
                pass

    if cal_ok is None:
        cal_ok, _why = calibration_sane_for_scoring(cal, hoop_px)

    # ---- 「向前找」优先 ----
    # 两条实现，**像素优先**：像素观测不经过标定，标定坏了也不受影响；
    # 球场坐标版只在标定通过校验时才有意义（否则等于拿随机数判队）。
    look = {}
    if lookback_s and getattr(rt, "player_obs", None):
        look = attribute_by_pixel_lookback(
            shots, rt.player_obs, hoop_px, lookback_s=float(lookback_s))
        att["meta"]["lookback_source"] = "pixel_obs"
    elif (lookback_s and getattr(rt, "player_track", None)
          and cal_ok):
        look = attribute_by_lookback(shots, getattr(rt, "players", {}) or {},
                                     rt.player_track, hoop_px,
                                     lookback_s=float(lookback_s))
        att["meta"]["lookback_source"] = "court_coords"
    elif lookback_s and getattr(rt, "player_track", None):
        att["meta"]["lookback_source"] = "skipped:calibration_invalid"

    for i, info in look.items():
        if not info:
            continue
        prev = att["by_shot"].setdefault(i, {})
        # 「向前找」是最后一步，它的结论（哪怕结论是"未定"）就是最终口径 ——
        # 理由栏只放**最终生效**的那条，把上一条移到 evidence，
        # 否则报告里会同时出现"筐下检不到人"和"判成客队"，自相矛盾。
        if prev.get("reason"):
            prev["evidence"] = prev.get("reason")
        prev["reason"] = info["reason"]
        prev["confidence"] = info["confidence"]
        if info.get("team"):
            prev["team"] = info["team"]
        prev["lookback"] = {"vote": info["vote"],
                            "lookback_s": info["lookback_s"],
                            "source": info.get("source", ""),
                            "d_min_px": info.get("d_min_px"),
                            "nearest": info["nearest"]}
    if lookback_s:
        att["meta"]["lookback_s"] = float(lookback_s)
        att["meta"]["lookback_used"] = sum(
            1 for v in look.values() if v.get("team"))
        att["meta"]["nearest"] = 0
        att["meta"]["unknown"] = sum(
            1 for v in att["by_shot"].values() if not v.get("team"))

    out = []
    for i, sh in enumerate(shots):
        info = att["by_shot"].get(i) or {}
        team = info.get("team") or ""
        if team not in ("home", "away"):
            # 判不出队别：**不猜**。为了不影响比分数字，仍按 hometown 记账，
            # 但在 value_source / 报告里明确标「未定」，绝不写成「这队得的」。
            team = hometown
            team_unknown = True
        else:
            team_unknown = False
        hx, hy = (0.0, -1.575) if side.get(team, "left") == "left" \
            else (0.0, 1.575)
        x, y, loc = hx, hy, "rim_placeholder"
        if cal_ok and cal is not None:
            try:
                xm, ym = cal.to_court(sh.exit_x, sh.exit_y)
                if abs(xm) <= 9.0 and abs(ym) <= 16.0:
                    x, y, loc = xm, ym, "ball_track"
            except Exception:
                pass
        val, vsrc = value_of(cal if loc == "ball_track" else None, x, y,
                             default_value=default_value)
        out.append(Attempt(
            t=round(float(sh.t), 2), team=team,
            player_id=info.get("player_id") or f"{team[0].upper()}?",
            x=round(float(x), 3), y=round(float(y), 3), period=1,
            is_free_throw=False, release_frame=int(sh.t * fps),
            made=True, conf=float(getattr(sh, "confidence", 0.6)),
            forced_value=int(val), source="hoopsight",
            location_source=loc, location_estimated=True,
            value_assumed=True,
            value_source=("team_unknown" if team_unknown else vsrc),
            counts_for_score=counts_for_score))
    out.sort(key=lambda a: a.t)
    return out, att
