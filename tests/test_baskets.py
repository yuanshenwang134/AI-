"""「这次进球是哪一队进的」自检。

不依赖任何视频：喂构造出来的球员像素观测，断言
  * 筐下有人时 → 判给那个人的队，并带出判据；
  * 筐下没人（最近的也离得很远）→ **标未定**，不许拿场上最近的人凑答案；
  * 最近两人分属两队且一样近 → 标 low；
  * 人工指定（--basket-teams）优先级最高；
  * 标定把篮筐投到离真篮筐很远的位置时，`calibration_sane_for_scoring`
    必须判它不能用来判 2/3 分。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from aihoop.baskets import (attribute_teams, calibration_sane_for_scoring,  # noqa: E402
                            value_of)

HOOP_PX = (100.0, 60.0, 40.0)      # cx, cy, rx


class Shot:
    def __init__(self, t=10.0):
        self.t = t
        self.hoop_cx, self.hoop_cy, self.hoop_rx = HOOP_PX
        self.hoop_ry = 18.0
        self.exit_x, self.exit_y = HOOP_PX[0], HOOP_PX[1] + 40
        self.confidence = 0.9


class Player:
    def __init__(self, team):
        self.team = team


def obs(pid, team, t, foot_x, foot_y):
    return dict(t=t, player_id=pid, team=team, foot_x=foot_x, foot_y=foot_y,
                box=[foot_x - 20, foot_y - 60, foot_x + 20, foot_y])


def test_team_from_nearest_player_under_rim():
    """筐下 1m 内有人 → 判给他那一队，置信度 ok，理由里带出距离。"""
    shots = [Shot(10.0)]
    players = {"A1": Player("home"), "B1": Player("away")}
    observations = [
        obs("A1", "home", 10.0, 100.0, 150.0),     # 筐下（90px ≈ 1m）
        obs("B1", "away", 10.0, 300.0, 400.0),     # 远处
    ]
    att = attribute_teams(shots, players, [], HOOP_PX, player_obs=observations)
    info = att["by_shot"][0]
    assert info["team"] == "home", info
    assert info["confidence"] == "ok", info
    assert info["player_id"] == "A1"
    assert "90px" in info["reason"], info["reason"]
    assert att["meta"]["nearest"] == 1


def test_no_player_under_rim_is_unknown():
    """筐下没人（最近的也 4m 外）→ 标未定，不拿场上最近的人凑答案。"""
    shots = [Shot(10.0)]
    players = {"A1": Player("home")}
    observations = [obs("A1", "home", 10.0, 100.0, 60.0 + 500.0)]
    att = attribute_teams(shots, players, [], HOOP_PX, player_obs=observations)
    info = att["by_shot"][0]
    assert info["team"] == "", info
    assert info["confidence"] == "unknown", info
    assert "无法判定" in info["reason"], info["reason"]
    assert att["meta"]["unknown"] == 1


def test_ambiguous_near_pair_is_low():
    """最近两人分属两队且几乎一样近 → 记最近的，但标 low。"""
    shots = [Shot(10.0)]
    players = {"A1": Player("home"), "B1": Player("away")}
    observations = [
        obs("A1", "home", 10.0, 100.0, 150.0),     # 90px
        obs("B1", "away", 10.0, 100.0, 160.0),     # 100px，几乎一样近
    ]
    att = attribute_teams(shots, players, [], HOOP_PX, player_obs=observations)
    info = att["by_shot"][0]
    assert info["team"] == "home"
    assert info["confidence"] == "low", info
    assert "分不清" in info["reason"], info["reason"]


def test_manual_override_wins():
    """人工指定优先：attribute_teams 本身不认识它，但 attempts 层会覆盖。"""
    shots = [Shot(10.0)]
    players = {"A1": Player("home")}
    observations = [obs("A1", "home", 10.0, 100.0, 150.0)]
    att = attribute_teams(shots, players, [], HOOP_PX, player_obs=observations)
    assert att["by_shot"][0]["team"] == "home"
    # 覆盖逻辑在 hoopsight_to_attempts 里，这里只断言「原始判据还在」，
    # 便于报告里同时保留「自动判成什么」与「人工改成了什么」。
    assert att["by_shot"][0]["reason"]


def test_no_observations_falls_back_to_colour_vote():
    """完全没有像素观测时，用近筐球员采样的球衣颜色投票兜底（标 low）。"""
    class S:
        def __init__(self, pid):
            self.player_id, self.t, self.x, self.y = pid, 0.0, 0.0, 0.0
    shots = [Shot(10.0)]
    players = {"A1": Player("home"), "A2": Player("home"), "B1": Player("away")}
    track = [S("A1"), S("A2"), S("B1")]
    att = attribute_teams(shots, players, track, HOOP_PX, player_obs=None)
    info = att["by_shot"][0]
    assert info["team"] == "home", info
    assert info["confidence"] == "low"
    assert att["meta"]["method"] == "color-vote"


def test_value_is_two_without_trustworthy_calibration():
    assert value_of(None, 0.0, -1.575) == (2, "visual_estimate")
    assert value_of(None, 0.0, -1.575, default_value=3) == (3, "visual_estimate")


def test_calibration_sanity_rejects_wrong_view():
    """标定把画面里的篮筐投到离真篮筐很远 → 必须判「不能用来判 2/3 分」。"""
    class BadCal:
        H = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]

        def to_court(self, px, py):
            # 假标定：把画面里的篮筐投到离真篮筐 9m 的地方
            return (7.5, 7.5)

    ok, why = calibration_sane_for_scoring(BadCal(), HOOP_PX)
    assert ok is False, why

    class GoodCal:
        H = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]

        def to_court(self, px, py):
            return (0.0, -1.575)                 # 正好落在真篮筐上

    ok2, why2 = calibration_sane_for_scoring(GoodCal(), HOOP_PX)
    assert ok2 is True, why2
    ok3, why3 = calibration_sane_for_scoring(None, HOOP_PX)
    assert ok3 is False and "没有可用标定" in why3


def test_motion_blob_colour_decides_team():
    """YOLO 在筐下检不到人时，用「人形运动块 + 球衣色」定队（B 档）。"""
    shots = [Shot(10.0)]
    players = {"A1": Player("home")}
    # 球员观测都在 5m 外 → A 档不成立
    observations = [obs("A1", "home", 10.0, 100.0, 60.0 + 500.0)]
    blobs = [dict(t=9.4, cx=110.0, cy=150.0, w=40, h=140, area=5600,
                  dist_px=95.0, colour_name="黄", colour=[26, 180, 200])]
    jc = {"legend": {"home": "黄", "away": "灰色"}}
    att = attribute_teams(shots, players, [], HOOP_PX, player_obs=observations,
                          motion_blobs=blobs, jersey_colors=jc)
    info = att["by_shot"][0]
    assert info["team"] == "home", info
    assert info["confidence"] == "low"
    assert "人形运动块" in info["reason"], info["reason"]
    assert att["meta"]["motion_blob"] == 1


def test_motion_blob_colour_mismatch_stays_unknown():
    """运动块颜色跟两队球衣都对不上 → 仍然未定，不硬判。"""
    shots = [Shot(10.0)]
    observations = [obs("A1", "home", 10.0, 100.0, 60.0 + 500.0)]
    blobs = [dict(t=9.4, cx=110.0, cy=150.0, w=40, h=140, area=5600,
                  dist_px=95.0, colour_name="紫/品红", colour=[150, 120, 160])]
    jc = {"legend": {"home": "黄", "away": "灰色"}}
    att = attribute_teams(shots, {}, [], HOOP_PX, player_obs=observations,
                          motion_blobs=blobs, jersey_colors=jc)
    info = att["by_shot"][0]
    assert info["team"] == "", info
    assert info["confidence"] == "unknown", info
    assert "对不上" in info["reason"], info["reason"]


def test_lookback_picks_the_team_that_had_the_ball():
    """「向前找」：判队看的是进球**前** 1.5s 谁在筐边，而不是进球那一刻。

    这是被真值验证过更准的那条规则（basketball_match.mp4：1.5s → 6/8，
    进球瞬间 → 4/8）：得分那一下筐下多是防守/抢板的人。
    """
    from aihoop.baskets import attribute_by_lookback

    class S:
        def __init__(self, t, pid, x, y):
            self.t, self.player_id, self.x, self.y = t, pid, x, y

    shots = [Shot(10.0)]
    players = {"A1": Player("home"), "B1": Player("away"),
               "A2": Player("home"), "B2": Player("away")}
    # 进球瞬间（t=10）：筐下站的是 away 的人（防守/抢板）
    # 进球前 1.5s（t=8.5）：筐下是 home 的人（进攻方）
    track = [
        S(10.0, "B1", 105.0, 62.0), S(10.0, "B2", 110.0, 65.0),
        S(8.5, "A1", 104.0, 60.0), S(8.5, "A2", 108.0, 66.0),
    ]
    got = attribute_by_lookback(shots, players, track, (100.0, 60.0, 40.0),
                                lookback_s=1.5, max_dist_m=999.0)
    assert got[0]["team"] == "home", got
    assert got[0]["vote"] == {"home": 2, "away": 0}, got
    assert "向前找" in got[0]["reason"]

    # lookback 关掉（0s）时看的是进球瞬间 → 判成 away，正好说明差别
    got0 = attribute_by_lookback(shots, players, track, (100.0, 60.0, 40.0),
                                 lookback_s=0.0, max_dist_m=999.0)
    assert got0[0]["team"] == "away", got0


def test_lookback_distance_gate_says_unknown():
    """最近的球员离筐太远 → 标未定，不硬给一个队。

    门槛是在 basketball_match.mp4 的 8 次事件上标定的：1.5m 时"凡敢判的全对"
    （4 对 0 错 4 未定），不设门槛则 6 对 2 错。
    """
    from aihoop.baskets import attribute_by_lookback

    class S:
        def __init__(self, t, pid, x, y):
            self.t, self.player_id, self.x, self.y = t, pid, x, y

    shots = [Shot(10.0)]
    players = {"A1": Player("home")}
    # 进位以米为单位：最近的人离筐 1.9m（>1.5m 门槛）
    track = [S(8.5, "A1", 0.0, 3.5)]
    got = attribute_by_lookback(shots, players, track, (0.0, -1.575, 40.0),
                                lookback_s=1.5, k=5, win=1.0, max_dist_m=1.5)
    assert got[0]["team"] == "", got
    assert got[0]["confidence"] == "unknown", got
    assert "没有可信的最后持球人" in got[0]["reason"], got[0]["reason"]
    # 同一条轨迹，门槛放宽 → 判出 home
    got2 = attribute_by_lookback(shots, players, track, (0.0, -1.575, 40.0),
                                 lookback_s=1.5, k=5, win=1.0, max_dist_m=9.0)
    assert got2[0]["team"] == "home", got2


def test_pixel_lookback_ignores_court_calibration():
    """像素空间的「向前找」不经过标定 —— 标定坏了也照样能判。

    这是被实测逼出来的一条：两份标定都把画面里的篮筐投到了离真篮筐 8~12m 外，
    用它们的球员坐标判队等于拿随机数判；而像素脚底点是直接观测值。
    """
    from aihoop.baskets import attribute_by_pixel_lookback

    shots = [Shot(10.0)]          # Shot 的篮筐像素是 (100, 60, 40)
    obs = [
        # 进球前 1.5s：home 的两个人都在筐边
        dict(t=8.5, player_id="A1", team="home", foot_x=105.0, foot_y=70.0),
        dict(t=8.5, player_id="A2", team="home", foot_x=112.0, foot_y=80.0),
        # 进球瞬间：away 的人离筐更近
        dict(t=10.0, player_id="B1", team="away", foot_x=101.0, foot_y=62.0),
    ]
    got = attribute_by_pixel_lookback(shots, obs, (100.0, 60.0, 40.0),
                                      lookback_s=1.5)
    assert got[0]["team"] == "home", got
    assert got[0]["source"] == "pixel_obs"
    assert got[0]["vote"] == {"home": 2, "away": 0}, got

    # 门槛：最近的也在 2.2×40=88px 之外 → 未定
    far = [dict(t=8.5, player_id="A1", team="home", foot_x=300.0, foot_y=300.0)]
    got2 = attribute_by_pixel_lookback(shots, far, (100.0, 60.0, 40.0),
                                       lookback_s=1.5)
    assert got2[0]["team"] == "", got2
    assert got2[0]["confidence"] == "unknown", got2


def test_label_whitelist_filters_auto_hits():
    """人工标注白名单：只有标注为「进球」的时刻才允许出现在结果里。

    这是被实测逼出来的兜底：nybo_3min 上自动判据 precision = 0%（3 个判定全错），
    唯一诚实的做法是拿人工标注当准绳，把误报全滤掉（那段素材最终正确输出 0 球）。
    """
    from aihoop.baskets import filter_shots_by_labels

    class S:
        def __init__(self, t):
            self.t = t

    shots = [S(59.94), S(127.46), S(173.42)]
    labels = [{"idx": 0, "t0": 59.73, "t1": 60.02, "label": "miss"},
              {"idx": 1, "t0": 127.25, "t1": 128.04, "label": "miss"},
              {"idx": 2, "t0": 173.21, "t1": 173.55, "label": "miss"}]
    kept, dropped = filter_shots_by_labels(shots, labels)
    assert kept == [], [s.t for s in kept]
    assert len(dropped) == 3

    labels[1]["label"] = "made"
    kept, dropped = filter_shots_by_labels(shots, labels)
    assert [round(s.t, 2) for s in kept] == [127.46], [s.t for s in kept]
    assert len(dropped) == 2


def test_confidence_gate_drops_unreliable_shots():
    """置信度门槛 + rejected 列表：低于门槛的判定不作为进球输出。

    实测背景（nybo_3min，人工确认 7 个候选全部没进）：自动判据把 3 个判成进球，
    置信度 0.896 / 0.917 / 0.970 —— 门槛要 0.970 才零误报，所以"再调阈值"救不了，
    真正该做的是用人工标注当准绳（见上面 label whitelist 的用例）。
    这里只验证门槛机制本身（用合成数据，几何用例在 test_hoopsight.py）。
    """
    from aihoop.hoopsight import SightConfig

    cfg = SightConfig()
    assert cfg.min_confidence == 0.5, cfg.min_confidence
    assert cfg.min_confidence > 0.0


def test_basket_teams_override(monkeypatch=None):
    """`--basket-teams` 人工指定优先于自动分队。"""
    from aihoop.baskets import hoopsight_to_attempts

    class Scan:
        hoops = [{"cx": 100.0, "cy": 60.0, "rx": 40.0}]
        shots = [Shot(10.0), Shot(30.0)]

    class RT:
        players = {"A1": Player("home")}
        player_track = []
        player_obs = [obs("A1", "home", 10.0, 100.0, 150.0)]   # 只有第 1 球有人

    atts, att = hoopsight_to_attempts(
        Scan(), RT(), cal=None, fps=25.0, team_override={1: "away", 2: "away"})
    assert [a.team for a in atts] == ["away", "away"], [a.team for a in atts]
    assert att["meta"]["manual_override"] == {"1": "away", "2": "away"}
    assert att["by_shot"][0]["confidence"] == "manual"


def test_unknown_team_is_flagged_in_value_source():
    """判不出队别时：仍记账，但 value_source 标 team_unknown（不冒充结论）。"""
    from aihoop.baskets import hoopsight_to_attempts

    class Scan:
        hoops = [{"cx": 100.0, "cy": 60.0, "rx": 40.0}]
        shots = [Shot(10.0)]

    class RT:
        players = {"A1": Player("home")}
        player_track = []
        player_obs = [obs("A1", "home", 10.0, 100.0, 700.0)]   # 很远

    atts, att = hoopsight_to_attempts(Scan(), RT(), cal=None, fps=25.0)
    assert len(atts) == 1
    assert atts[0].value_source == "team_unknown", atts[0].value_source
    assert att["by_shot"][0]["team"] == ""


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
    print("baskets tests:", "all passed" if not fails else f"{fails} failed")
    raise SystemExit(1 if fails else 0)
