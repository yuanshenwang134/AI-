"""战报生成 —— 套餐 A 的「导出」需求。

两条路：
  1. build_report_md / build_report_json  —— 规则模板，零依赖，秒出
  2. build_report_llm()                   —— 可选接 LLM（套餐 C 的能力，
                                             套餐 A 里留好接口即可）

模板战报的好处：完全离线、可复现、无 API Key 也能答辩。
LLM 版本只是把同一份结构化 report_json 喂给模型润色成更「像人写的」文案。
"""
from __future__ import annotations

import json
import math
from typing import Optional

from .model import Shot, ShotValue
from .rules import zone_of


def build_report_json(game: dict, players: list[dict], shots: list[Shot],
                      tactics: Optional[dict] = None) -> dict:
    home, away = game["teams"]["home"], game["teams"]["away"]
    hs, as_ = home["stats"], away["stats"]

    def top(rows, key, n=3):
        return [{"player": r["name"], "value": r[key]} for r in
                sorted(rows, key=lambda x: -x[key])[:n] if r_has(r, key)]

    def r_has(r, k):
        return r.get(k, 0) > 0

    return {
        "title": f"{home['name']} vs {away['name']} 比赛分析报告",
        "final_score": {"home": game["score"]["home"], "away": game["score"]["away"]},
        "clip_score": game.get("clip_score") or {"home": 0, "away": 0},
        "score_policy": game.get("score_policy", "scoreboard"),
        "scoreboard_reference": game.get("scoreboard_reference")
        or game.get("carry_in") or {},
        "winner": home["name"] if game["score"]["home"] > game["score"]["away"]
        else (away["name"] if game["score"]["away"] > game["score"]["home"] else "平局"),
        "margin": abs(game["score"]["home"] - game["score"]["away"]),
        "quarter_scores": game["quarter_scores"],
        "team_stats": {"home": hs, "away": as_},
        "leaders": {
            "home": {"scoring": top([p for p in players if p["team"] == "home"], "points"),
                     "rebounds": top([p for p in players if p["team"] == "home"], "reb"),
                     "assists": top([p for p in players if p["team"] == "home"], "ast")},
            "away": {"scoring": top([p for p in players if p["team"] == "away"], "points"),
                     "rebounds": top([p for p in players if p["team"] == "away"], "reb"),
                     "assists": top([p for p in players if p["team"] == "away"], "ast")},
        },
        "shooting": {
            "home": {k: hs[k] for k in ("fgm", "fga", "fg_pct", "tpm", "tpa", "tp_pct",
                                        "ftm", "fta", "points")},
            "away": {k: as_[k] for k in ("fgm", "fga", "fg_pct", "tpm", "tpa", "tp_pct",
                                         "ftm", "fta", "points")},
        },
        "tactics": _compact_tactics(tactics),
        "hot_zones": _hot_zones(shots),
        "key_shots": _key_shots(shots, game),
        "carry_in": game.get("carry_in") or {},
        "confidence": {
            "total": len(shots),
            "needs_review": len(game.get("needs_review", [])),
            "rate": round(1 - len(game.get("needs_review", [])) / max(1, len(shots)), 3),
        },
    }


def _hot_zones(shots: list[Shot], n: int = 4) -> list[dict]:
    agg: dict[str, dict] = {}
    for s in shots:
        if s.result == "unknown":
            continue
        # 位置未知的出手不进热区：占位坐标全贴在篮下，会把热区算成"全在禁区"，
        # 与战报里"没有球场坐标就不给热区"的说法自相矛盾（`rules.shot_chart` 早就这么挡了）。
        if not s.location_known:
            continue
        z = zone_of(s.x, s.y)
        d = agg.setdefault(z, {"zone": z, "att": 0, "made": 0, "points": 0})
        d["att"] += 1
        d["made"] += 1 if s.made else 0
        d["points"] += s.score_points
    for d in agg.values():
        d["pct"] = round(d["made"] / d["att"], 3) if d["att"] else 0.0
        d["pps"] = round(d["points"] / d["att"], 2) if d["att"] else 0.0
    # 按「每回合得分」排序，最少 3 次出手才纳入
    return sorted([d for d in agg.values() if d["att"] >= 3],
                  key=lambda d: -d["pps"])[:n]


def _key_shots(shots: list[Shot], game: dict, n: int = 5) -> list[dict]:
    """关键球：第四节最后 3 分钟内的得分球 + 三分。"""
    cand = [s for s in shots if s.made and
            (s.period >= 4 or s.value == ShotValue.THREE.value)]
    cand.sort(key=lambda s: (-s.period, -s.value, s.t))
    # 位置未知的出手仍是真实事件，保留在关键球里，但**不给假的区域/距离**
    # （占位坐标算出来必然是"禁区 0 米"，会让战报和前端表格一起说谎）。
    return [{"t": round(s.t, 1), "period": s.period, "player_id": s.player_id,
             "team": s.team, "value": s.value,
             "zone": zone_of(s.x, s.y) if s.location_known else "位置未知",
             "distance": round(s.distance, 2) if s.location_known else None}
            for s in cand[:n]]


def build_report_md(game: dict, players: list[dict], shots: list[Shot],
                    zones_by_team: Optional[dict] = None,
                    tactics: Optional[dict] = None) -> str:
    r = build_report_json(game, players, shots, tactics)
    home, away = game["teams"]["home"], game["teams"]["away"]
    hs, as_ = home["stats"], away["stats"]
    L = []
    A = L.append

    A(f"# {r['title']}")
    A("")
    jg = game.get("judgement") or {}
    if jg.get("state") == "cannot_judge":
        # 判不出来时**不能**只甩一个 0:0 了事 —— 那会被读成「双方都没得分」。
        A("> ⚠ **本片段未能判定比分。** 系统没有识别到任何得分事件，原因：")
        for line in jg.get("reasons", []) or ["（未提供原因）"]:
            A(f"> - {line}")
        A("> ")
        A("> 下面的 **0 : 0 只表示「没有检测到得分」，不等于双方真的都没得分**。")
        A("> 如果这段素材里确实有进球，请在「复核页」人工补录，"
          "或者针对这个频道重跑一次比分牌模板标定。")
        A("")
    policy = str(game.get("score_policy", "scoreboard") or "scoreboard").lower()
    # 带入分到底算没算进总分 —— 直接看管线给的结论，别再用 policy 去猜：
    # auto 口径下"比分牌读出来了"就计入、没读出来就不计入，同一个 policy 两种结果。
    carry_counted = bool(game.get("carry_counted"))
    if policy == "scoreboard":
        court_mode = False         # 显式用比分牌口径：报告按"最终比分"写
    elif policy in ("court", "visual"):
        court_mode = True
    else:                          # auto：读出来就算入，没读出来就按场上进球
        court_mode = not carry_counted
    carry = game.get("carry_in") or {}
    has_ref = bool(carry.get("home") or carry.get("away"))
    clip = game.get("clip_score") or {"home": 0, "away": 0}
    if court_mode:
        A(f"**本片段得分：{home['name']} {clip['home']} : "
          f"{clip['away']} {away['name']}**  ")
        if has_ref:
            A(f"比分牌读数（仅供参考，未计入本片段得分）：**{home['name']} "
              f"{int(carry.get('home', 0) or 0)} : {int(carry.get('away', 0) or 0)} "
              f"{away['name']}**  ")
        if clip['home'] != clip['away']:
            leader = home['name'] if clip['home'] > clip['away'] else away['name']
            A(f"本片段领先：**{leader}**，领先 {abs(clip['home'] - clip['away'])} 分。")
        elif not (clip['home'] or clip['away']):
            A("本片段：**双方均未得分**。")
        else:
            A("本片段：**战平**。")
        A("")
        A("> 注意：上方大字是**本片段按场上检测到的进球计算的得分**；"
          "电视比分牌的读数仅作参考，**没有计入本片段得分**。")
    elif has_ref:
        A(f"**本片段得分：{home['name']} {clip['home']} : "
          f"{clip['away']} {away['name']}**  ")
        A(f"比分牌带入的比赛当前大比分：**{home['name']} "
          f"{int(carry.get('home', 0) or 0)} : {int(carry.get('away', 0) or 0)} "
          f"{away['name']}**  ")
        A(f"当前大比分（带入 + 本片段）：**{home['name']} "
          f"{r['final_score']['home']} : {r['final_score']['away']} "
          f"{away['name']}**  ")
        if r['final_score']['home'] != r['final_score']['away']:
            leader = home['name'] if r['final_score']['home'] > r['final_score']['away'] \
                else away['name']
            A(f"比赛当前领先：**{leader}**，领先 {r['margin']} 分。")
        else:
            A("比赛当前：**战平**。")
        A("")
        A(f"> 注意：上方大字是**本片段得分**；比分牌带入的 "
          f"{int(carry.get('home', 0) or 0)} : {int(carry.get('away', 0) or 0)} "
          "是比赛进行到该时刻的已有比分，**不是这"
          f" {_mmss(game.get('duration', 0))} 产生的**。")
    else:
        A(f"**最终比分：{home['name']} {r['final_score']['home']} : "
          f"{r['final_score']['away']} {away['name']}**  ")
        A(f"胜者：**{r['winner']}**，分差 {r['margin']} 分。")
    if any("value_estimated" in (s.tags or []) for s in shots):
        A("> ℹ 本片段的 2/3 分值是**视觉估计**（没有可用的球场标定，也没有比分牌"
          "跳变作为依据）。这些出手已经在「复核页」列出，可以手动改成 2 分或 3 分。")
    n_outcome_unknown = sum(s.result == "unknown" for s in shots)
    if n_outcome_unknown:
        A(f"> 有 **{n_outcome_unknown}** 次出手的结果待确认，暂不计入命中率及得分；以下统计仅依据已有判定。")
    n_team_unknown = sum(1 for s in shots if "team_unknown" in (s.tags or []))
    if n_team_unknown:
        A(f"> ⚠️ **有 {n_team_unknown} 次进球的「哪一队进的」判不出来**：这段素材里"
          "在篮筐附近既没检出球员、也没检出人形比例的运动块，代码没有拿"
          "「场上最近的人」凑答案，只把证据留了下来。上面的比分是把这些球"
          "**暂记在主队名下**后的数字 —— 谁得的并不确定，"
          "请用 `--basket-teams \"1=home,2=away\"` 人工指定，"
          "或看产物目录里的 `hoopsight_team.png` / `hoopsight_people.png` 核对。")
        A("")
    A("")
    unmatched = game.get("unmatched_goals") or []
    if unmatched:
        A(f"> ⚠ 视觉路径另外检测到 **{len(unmatched)} 次命中**，但当前计分口径"
          "没有把它算进本片段得分；命中已保留在高光和出手明细里，可在复核页确认。")
        A("")
    A("## 一、分节比分")
    A("")
    A("| 节次 | " + home["name"] + " | " + away["name"] + " |")
    A("|---|---|---|")
    for q in r["quarter_scores"]:
        A(f"| 第{q['period']}节 | {q['home']} | {q['away']} |")
    if (not court_mode) and has_ref:
        A(f"| 本片段 | {clip['home']} | {clip['away']} |")
    total_label = "本片段总计" if court_mode else ("当前大比分" if has_ref else "总计")
    A(f"| **{total_label}** | **{r['final_score']['home']}** | "
      f"**{r['final_score']['away']}** |")
    if court_mode and has_ref:
        A("")
        A(f"> 比分牌读数为 {int(carry.get('home', 0) or 0)} : "
          f"{int(carry.get('away', 0) or 0)}，这是比赛进行到该时刻的已有比分，"
          "仅供参考，不计入本片段得分。")
    elif has_ref:
        A("")
        A(f"> 第{int(game.get('base_period', 1) or 1)}节的 "
          f"{int(carry.get('home', 0) or 0)} : {int(carry.get('away', 0) or 0)} "
          "包含比分牌带入分；本片段实际得分为 "
          f"**{clip['home']} : {clip['away']}**。")
    A("")

    A("## 二、球队投篮数据")
    A("")
    A(f"| 指标 | {home['name']} | {away['name']} |")
    A("|---|---|---|")
    if court_mode:
        A(f"| 本片段得分 | {clip['home']} | {clip['away']} |")
        if has_ref:
            A(f"| 比分牌读数（参考） | {int(carry.get('home', 0) or 0)} | "
              f"{int(carry.get('away', 0) or 0)} |")
    elif has_ref:
        A(f"| 本片段得分 | {clip['home']} | {clip['away']} |")
        A(f"| 当前大比分 | {r['final_score']['home']} | {r['final_score']['away']} |")
    else:
        A(f"| 总得分 | {hs['points']} | {as_['points']} |")
    A(f"| 投篮 | {hs['fgm']}/{hs['fga']} ({hs['fg_pct']*100:.1f}%) | "
      f"{as_['fgm']}/{as_['fga']} ({as_['fg_pct']*100:.1f}%) |")
    A(f"| 三分 | {hs['tpm']}/{hs['tpa']} ({hs['tp_pct']*100:.1f}%) | "
      f"{as_['tpm']}/{as_['tpa']} ({as_['tp_pct']*100:.1f}%) |")
    A(f"| 罚球 | {hs['ftm']}/{hs['fta']} | {as_['ftm']}/{as_['fta']} |")
    A("")
    if court_mode and has_ref:
        A(f"> 注：比分牌显示 **{int(carry.get('home', 0) or 0)} : "
          f"{int(carry.get('away', 0) or 0)}**，这是比赛进行到该时刻的已有比分，"
          "仅供参照；本报告按场上检测到的进球计分。")
        A("")
    elif has_ref:
        A(f"> 注：本片段从比赛中途开始，比分牌上已有 "
          f"**{int(carry.get('home', 0) or 0)} : {int(carry.get('away', 0) or 0)}**，"
          "这部分分数计入总分，但没有对应的出手事件，"
          "所以出手统计只覆盖本片段内真实发生的回合。")
        A("")
    A(_analysis_paragraph(hs, as_, home["name"], away["name"], game=game))
    A("")

    A("## 三、得分榜")
    A("")
    A("| 球员 | 球队 | 得分 | 投篮 | 三分 | 罚球 | 篮板 | 助攻 |")
    A("|---|---|---|---|---|---|---|---|")
    for p in players[:12]:
        A(f"| {p['name']} | {p['team']} | {p['points']} | "
          f"{p['fgm']}/{p['fga']} | {p['tpm']}/{p['tpa']} | "
          f"{p['ftm']}/{p['fta']} | {p['reb']} | {p['ast']} |")
    A("")

    located = [s for s in shots if "location_unknown" not in s.tags]
    if r["hot_zones"] and located:
        A("## 四、高效出手区域")
        A("")
        A("| 区域 | 出手 | 命中 | 命中率 | 每次出手得分 |")
        A("|---|---|---|---|---|")
        for z in r["hot_zones"]:
            A(f"| {z['zone']} | {z['att']} | {z['made']} | "
              f"{z['pct']*100:.1f}% | {z['pps']} |")
        A("")
    elif r["hot_zones"]:
        A("## 四、高效出手区域")
        A("")
        A("> 本视频**没有可用的球场坐标**（缺少这个机位的标定），"
          "所以无法给出出手热区 —— 与其画一张全是「禁区」的假热区，不如空着。")
        A("")

    A("## 五、关键球")
    A("")
    for k in r["key_shots"]:
        A(f"- 第{k['period']}节 {_mmss(k['t'])} — 球员 {k['player_id']} "
          f"在 {k['zone']}（距篮 {k['distance']}m）命中 {k['value']} 分球")
    A("")

    A("## 六、数据可信度")
    A("")
    c = r["confidence"]
    A(f"- 自动识别出手共 **{c['total']}** 次，其中 **{c['needs_review']}** 次"
      f"置信度偏低已进入人工复核，自动判定准确率参考值 "
      f"**{c['rate']*100:.1f}%**。")
    A("- 低置信度事件在「复核页」一键修正，修正结果会写回统计口径。")
    unknown = sum(1 for s in shots if "location_unknown" in s.tags)
    if unknown and located:
        A(f"- ⚠ **{unknown}** 次出手的位置是**占位估计**（球没被追踪到，只能记在"
          "被进攻篮筐附近）。这些出手的**分值、命中与否、比分都是准的**，"
          "但热区图上的落点不真实，热区结论请以人工复核为准。")
    elif unknown:
        A(f"- 全部 **{unknown}** 次出手都没有可用的球场坐标（缺这个机位的标定），"
          "所以战报只给比分与命中数，不给热区。"
          "**比分与命中判定不依赖坐标**，不受这条限制影响。")
    ev = _evidence_lines(game.get("meta") or {})
    if ev:
        A("")
        A("**本次判定用到的证据：**")
        for line in ev:
            A(f"- {line}")
    A("")
    _tactics_section(A, game, tactics)
    A("---")
    A("")
    A("*本报告由 AI 篮球分析软件自动生成：计分规则引擎负责 1/2/3 分判定与"
      "命中融合，统计口径与热区划分均为自研实现。*")
    return "\n".join(L)


def _compact_tactics(tactics: Optional[dict]) -> dict:
    """report.json 里只保留战术摘要。

    为什么不能直接塞整份 tactics.json：它有几百 KB 到 1 MB（含逐帧的空间曲线
    和全部传球事件），而 report.json 是要被 /export?fmt=json 下载、被前端战报页
    整体加载的。把逐帧数据塞进去只会让"想看战报"变成"下 2 MB"。
    完整数据在 tactics.json / tactics_frames.jsonl，需要的人自己去取。
    """
    if not (tactics and tactics.get("available")):
        return {}
    poss = dict(tactics.get("possession") or {})
    poss.pop("spells", None)
    poss.pop("list", None)
    passes = dict(tactics.get("passes") or {})
    passes.pop("events", None)
    return {
        "available": True,
        "attack_side": tactics.get("attack_side"),
        "possession": poss,
        "passes": passes,
        "pass_network": tactics.get("pass_network"),
        "formation": tactics.get("formation"),
        "spacing": {"teams": (tactics.get("spacing") or {}).get("teams")},
        "insights": tactics.get("insights"),
        "coverage": tactics.get("coverage"),
    }


def _tactics_section(A, game: dict, tactics: Optional[dict]) -> None:
    """套餐 B 的战术章节。没有战术数据时也写清楚"为什么没有"。

    这一段存在的意义：答辩时评委看的是**报告**，不是前端页面。
    把"传球网络 / 阵型 / 空间"写进战报，套餐 B 的增量才落到了纸面上。
    """
    A("## 七、战术分析（自研战术层）")
    A("")
    hn = game["teams"]["home"]["name"]
    an = game["teams"]["away"]["name"]
    if not (tactics and tactics.get("available")):
        # 优先用**管线给出的具体原因**（例如"自动逐帧标定没通过独立校验：
        # 人工标的篮筐经它投到离真筐 6.60 m"），它比一句"没有球员轨迹"有用得多。
        reason = ((game.get("tactics") or {}).get("reason")
                  or (tactics or {}).get("reason")
                  or "本场没有球员轨迹（缺少球场标定或球员检测被关闭）")
        A(f"- 本场未生成战术分析：{reason}")
        A("- 战术层需要「球员逐帧球场坐标」。真视频路径要么有一份**通过校验**"
          "的球场标定，要么自动逐帧标定同时通过质量门槛（达标率 ≥0.5、中位 "
          "ratio ≥1.25）**与独立校验**（人工标的篮筐投影必须落在真篮筐 3m 内）；"
          "任一不过就不出战术图，而不是给一张错的。"
          "修法：在「上传与分析」页重新标一次球场 —— 建议**在同一帧里点 6 个以上"
          "不在同一条线上**的特征点。合成数据源默认自带球员轨迹。")
        A("")
        return

    if tactics.get("position_unverified"):
        A(f"> ⚠ **球员位置未独立校验**：{tactics.get('position_note') or ''}")
        A(">")
        A("> 也就是说：回合/传球/阵型/间距这些**基于相对位置**的结论可以参考，"
          "但具体坐标可能有系统偏差；要用于答辩的硬结论，请先把这个机位的球场"
          "标定重标一次。")
        A("")

    poss = tactics.get("possession") or {}
    passes = tactics.get("passes") or {}
    pt = poss.get("by_team") or {}
    A(f"- **回合**：共 **{poss.get('total', 0)}** 回合"
      f"（{hn} {pt.get('home', 0)} / {an} {pt.get('away', 0)}），"
      f"平均每回合 **{poss.get('avg_passes', 0)}** 次传球、"
      f"平均回合时长 {poss.get('avg_duration', 0)}s。")
    A(f"- **传球**：识别成功传球 **{passes.get('total', 0)}** 次，"
      f"失误/抢断 **{passes.get('turnovers', 0)}** 次，另有 "
      f"**{passes.get('possession_changes', 0)}** 次「投篮之后的球权转换」"
      f"（投进 / 打铁后的换手，不计为失误）。传球与失误都由「控球人切换」推出，"
      f"口径是可解释的几何规则。")
    side = {"left": "左半场", "right": "右半场", "mixed": "左右两侧"}
    A(f"- **进攻方向**：{side.get(tactics.get('attack_side', ''), '-')}"
      "（战术层把两侧进攻折叠到同一套半场坐标后再比较阵型）。")
    A("")

    A("**传球网络（前 3 组二人连线）**")
    A("")
    net = tactics.get("pass_network") or {}
    for team, tname in (("home", hn), ("away", an)):
        g = net.get(team) or {}
        edges = (g.get("edges") or [])[:3]
        if not edges:
            A(f"- {tname}：没有识别到队内传球。")
            continue
        names = {n["id"]: n.get("name", n["id"]) for n in (g.get("nodes") or [])}
        parts = [f"{names.get(e['source'], e['source'])} → "
                 f"{names.get(e['target'], e['target'])} ({e['value']} 次)"
                 for e in edges]
        A(f"- {tname}：" + "；".join(parts))
    A("")

    A("**阵型识别（按时间占比最高的两档）**")
    A("")
    A("| 球队 | 主要进攻落位 | 主要防守阵型 |")
    A("|---|---|---|")
    for team, tname in (("home", hn), ("away", an)):
        summ = ((tactics.get("formation") or {}).get("summary") or {}).get(team) or {}
        off = "、".join(x["label"] for x in (summ.get("offense") or [])[:2]) or "-"
        deff = "、".join(x["label"] for x in (summ.get("defense") or [])[:2]) or "-"
        A(f"| {tname} | {off} | {deff} |")
    A("")

    A("**空间指标**")
    A("")
    A("| 球队 | 凸包面积(m²) | 平均间距(m) | 阵型宽度(m) | 纵深(m) | 重心到篮筐(m) |")
    A("|---|---|---|---|---|---|")
    sp = (tactics.get("spacing") or {}).get("teams") or {}
    for team, tname in (("home", hn), ("away", an)):
        d = sp.get(team) or {}
        A(f"| {tname} | {d.get('area', '-')} | {d.get('mean_dist', '-')} | "
          f"{d.get('width', '-')} | {d.get('depth', '-')} | {d.get('radius', '-')} |")
    A("")
    A("> 空间指标都是「把球员位置投到球场平面后」的纯几何量：凸包面积越大"
      "说明进攻拉得越开；平均间距衡量整体疏密；"
      "纵深反映是压到篮下还是拉开在外线。")
    A("")

    ins = tactics.get("insights") or []
    if ins:
        A("**战术亮点**")
        A("")
        for i in ins[:6]:
            A(f"- {i.get('text', '')}")
        A("")
    notes = tactics.get("notes") or []
    if notes:
        A("> 说明：")
        for n in notes:
            A(f"> - {n}")
        A("")


def _evidence_lines(meta: dict) -> list[str]:
    """把「这次用了哪几路证据」翻成人话，写进战报。"""
    out = []
    sb = meta.get("scoreboard") or {}
    if sb and sb.get("frames_read"):
        hit, read = sb.get("frames_hit") or 0, sb.get("frames_read") or 0
        if hit:
            out.append(f"**广播比分牌**：读取成功 {hit}/{read} 帧"
                       f"（{hit / max(1, read) * 100:.0f}%），比分与得分时刻由它给出")
        else:
            out.append(f"**广播比分牌**：一帧都没读出来（0/{read}）。"
                       "要么这段视频没有比分牌覆盖层，要么它的台标字体和已标定的"
                       "模板不匹配 —— 后者需要针对这个频道重跑一次 "
                       "`scripts/build_scoreboard_templates.py` 标定")
    if meta.get("scoreboard_final"):
        f = meta["scoreboard_final"]
        out.append(f"比分牌读到的最终比分：**{f.get('home')} : {f.get('away')}**")
    vis = meta.get("visual") or {}
    if vis:
        out.append(f"**视觉路径（球 + 篮筐）**：{vis.get('method', '')}；"
                   f"从球轨迹里识别出 {vis.get('shots')} 次出手、"
                   f"判进 {vis.get('made')} 次")
        if vis.get("hoop_drift_px") and max(vis["hoop_drift_px"]) > 6:
            out.append(f"机位在移动：篮筐在整段里漂移了 "
                       f"{vis['hoop_drift_px'][0]}×{vis['hoop_drift_px'][1]} 像素，"
                       "判定用的是**每一帧各自的篮筐位置**")
        out.append("命中判定口径：球在图像里**从篮筐平面上方落到下方**，"
                   "且穿越点落在篮圈内 —— 不需要知道球离地多高，"
                   "所以不受单目测高误差影响")
    ball = meta.get("ball")
    if ball:
        out.append(f"**球轨迹**：候选 {ball.get('candidates', 0)} 个、"
                   f"连成 {ball.get('tracks', 0)} 条轨迹、"
                   f"保留 {ball.get('kept_points', 0)} 个点")
    at = meta.get("attempts") or {}
    if at:
        out.append(f"出手来源：比分牌 {at.get('from_scoreboard', 0)} 次、"
                   f"球轨迹 {at.get('from_ball_track', 0)} 次")
    if not meta.get("calibration_valid", True):
        out.append("⚠ 本视频**没有可用的球场标定**（画面尺寸/场地与已标定的机位不同），"
                   "所以出手位置无法换算成球场坐标；**每次得分的分值按默认值计**"
                   "（单目图像距离无法可靠区分 2 分和 3 分）。"
                   "要精确到 2/3 分，需要先用 `aihoop.cli calibrate` 标定这个机位。")
    teams = meta.get("teams")
    if teams:
        out.append(f"**分队依据**：球衣颜色聚类（绿 {teams.get('green_hue')}° / "
                   f"红 {teams.get('red_hue')}°，{teams.get('players')} 名球员）")
    if meta.get("scoreboard_error"):
        out.append("比分牌未启用或读取失败："
                   + str(meta["scoreboard_error"]).split("\n")[0][:100])
    if meta.get("visual_error"):
        out.append(f"视觉路径未启用：{meta['visual_error']}")
    return out


def _analysis_paragraph(hs: dict, as_: dict, hn: str, an: str,
                        game: Optional[dict] = None) -> str:
    """挑出最有信息量的 2~3 条差异，写成一段人话。

    注意要处理好一个尴尬但常见的情况：**赢球的一方命中率反而更低**。
    这时候不能只报「谁命中率高」，必须点出「多出来的出手次数」才是胜负手 ——
    否则战报读起来像在自相矛盾。
    """
    game = game or {}
    policy = str(game.get("score_policy", "scoreboard") or "scoreboard").lower()
    if policy == "scoreboard":
        court_mode = False
    elif policy in ("court", "visual"):
        court_mode = True
    else:
        court_mode = not bool(game.get("carry_counted"))
    ref = game.get("scoreboard_reference") or game.get("carry_in") or {}
    has_ref = bool(ref.get("home") or ref.get("away"))
    clip = game.get("clip_score") or {"home": hs["points"], "away": as_["points"]}
    diff = int(clip.get("home", 0)) - int(clip.get("away", 0))
    if court_mode:
        if diff == 0:
            lead = ("本片段双方均未得分"
                    if not (clip.get("home") or clip.get("away"))
                    else "本片段双方战平")
        else:
            lead = (f"本片段 {hn} 领先 {diff} 分" if diff > 0
                    else f"本片段 {an} 领先 {-diff} 分")
        if has_ref:
            lead += (f"；比分牌读数 {int(ref.get('home', 0) or 0)} : "
                     f"{int(ref.get('away', 0) or 0)} 仅供参考，不计入本片段得分")
    elif has_ref:
        if diff == 0:
            lead = ("本片段双方均未得分"
                    if not (clip.get("home") or clip.get("away"))
                    else "本片段双方战平")
        else:
            lead = (f"本片段 {hn} 领先 {diff} 分" if diff > 0
                    else f"本片段 {an} 领先 {-diff} 分")
        th = game.get("score", {}).get("home", 0)
        ta = game.get("score", {}).get("away", 0)
        lead += f"；比赛当前大比分 {hn} {th} : {ta} {an}"
    else:
        lead = (f"{hn}最终以 {diff} 分优势取胜" if diff > 0 else
                f"{an}最终以 {-diff} 分优势取胜" if diff < 0 else "双方战平")
    winner_is_home = diff >= 0
    winner = hn if winner_is_home else an
    loser = an if winner_is_home else hn
    w, l = (hs, as_) if winner_is_home else (as_, hs)

    parts = []
    d_fg = (w["fg_pct"] - l["fg_pct"]) * 100
    if diff != 0 and d_fg < -3:
        # 赢了但命中率更低 —— 说明赢在出手量/失误控制
        extra = w["fga"] - l["fga"]
        if extra > 0:
            parts.append(f"{winner}虽然命中率低了 {abs(d_fg):.1f} 个百分点，"
                         f"但多出手 {extra} 次，靠出手量把分差补了回来")
        else:
            parts.append(f"{winner}尽管命中率低了 {abs(d_fg):.1f} 个百分点，"
                         f"仍凭借罚球与关键球拿下比赛")
    elif abs(d_fg) >= 3:
        better = hn if d_fg > 0 else an
        parts.append(f"{better}的整体命中率高出 {abs(d_fg):.1f} 个百分点")

    d_tp = hs["tpm"] - as_["tpm"]
    if abs(d_tp) >= 2:
        better = hn if d_tp > 0 else an
        parts.append(f"{better}多命中 {abs(d_tp)} 记三分球")

    d_fta = hs["fta"] - as_["fta"]
    if abs(d_fta) >= 5:
        better = hn if d_fta > 0 else an
        parts.append(f"{better}获得更多罚球机会（多 {abs(d_fta)} 次）")

    if not parts:
        parts.append("双方在投篮效率上极为接近，胜负更多取决于失误与篮板球")
    return "**技术分析：**" + lead + "。" + "；".join(parts) + "。"


def _mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


# --------------------------------------------------------------------------
# 可选：LLM 润色（套餐 C 能力，A 里留接口）
# --------------------------------------------------------------------------
LLM_PROMPT = """你是一名专业篮球解说。下面是一场业余比赛的自动统计数据（JSON）。
请写一篇 400~600 字的中文比赛战报，要求：
1. 开头交代比分与胜负走势；2. 中段分析双方投篮效率差异与关键球员；
3. 结尾点出决定比赛走向的 2~3 个技术环节。
不要编造 JSON 里不存在的数据（如球员姓名不确定就用编号）。

数据：
{payload}
"""


def build_report_llm(report_json: dict, api_key: Optional[str] = None,
                     model: str = "deepseek-chat",
                     base_url: str = "https://api.deepseek.com") -> str:
    """调用 OpenAI 兼容接口（DeepSeek / 通义）生成战报。

    没有 API Key 时直接抛错并提示用模板战报 —— 套餐 A 不依赖这一步。
    """
    import os
    import urllib.request

    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未设置 DEEPSEEK_API_KEY。模板战报无需 LLM："
            "请在战报导出中选择「模板战报」（report.md），已可直接使用。")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user",
                      "content": LLM_PROMPT.format(
                          payload=json.dumps(report_json, ensure_ascii=False))}],
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        d = json.loads(resp.read().decode())
    return d["choices"][0]["message"]["content"]
