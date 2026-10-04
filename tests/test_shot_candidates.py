"""候选出手层（"可能漏检的出手"）与球可见性摘要：只提示、不计分。

为什么单独测这一层：用户明确要求"**保留现有判定**，额外列出可能漏检的出手及回看时间，
**不直接加入投篮次数或命中统计**；同时补上球可见性提示"。所以这里守三条不变量：

1. 候选**不进入** events、不改变任何判定结果；
2. 候选与球可见性只读已录的逐帧依据，旧 trace（缺字段）返回 None 而不是编造数字；
3. 候选的形态判据（滚动窗口顶点 + 轨迹接近过篮筐 + 起点离筐分类）按预期工作。
"""
import unittest

from aihoop.legacy_shots.stream import (LegacyShotStream, DEFAULT_CONFIG, ball_visibility,
                                        shot_candidates)


def frame(t, ball=None, predicted=False, rim_fresh=True, rim=(100.0, 50.0, 40.0)):
    """造一帧：ball=(x, y, w) 或 None；rim=(cx, cy, w)。"""
    row = {"frame": int(round(t * 30)), "t": t, "cut": False, "persons": []}
    if ball is None:
        row["ball"] = None
    else:
        x, y, w = ball
        row["ball"] = {"cls": "basketball", "conf": .9, "xyxy": [x - w / 2, y - w / 2,
                                                                 x + w / 2, y + w / 2],
                       "track_id": None, "predicted": predicted}
    if rim_fresh and rim:
        cx, cy, w = rim
        row["tracked_rim"] = {"cx": cx, "cy": cy, "w": w, "h": w * .3, "segment": 1,
                              "fresh": True, "last_seen_t": t}
    else:
        row["tracked_rim"] = {"cx": rim[0] if rim else 0, "cy": rim[1] if rim else 0,
                              "w": rim[2] if rim else 0, "h": 12.0, "segment": 1,
                              "fresh": False, "last_seen_t": 0.0}
    return row


RIM = (1000.0, 60.0, 40.0)      # 筐：中心 (1000, 60)，宽 40px


def arc(start_t, back_px, rise=240.0, n=12, dt=1 / 30, y_start=300.0, y_end=None):
    """造一条"从远处上升、然后下落到筐附近"的弧线。

    back_px = 起手点离筐心的横向距离（像素）—— 决定这是"远投类"还是"近筐类"。
    """
    cx, cy, w = RIM
    apex = n // 2
    y_end = y_start if y_end is None else y_end
    rows = []
    for i in range(n):
        t = start_t + i * dt
        frac = i / apex
        y = (y_start - (y_start - cy) * frac) if i <= apex else (cy + (y_end - cy) * (frac - 1))
        x = (cx - back_px) + back_px * (i / (n - 1))
        rows.append(frame(t, (x, y, 12.0), rim=RIM))
    return rows


class ShotCandidates(unittest.TestCase):
    def test_arc_becomes_candidate_without_touching_events(self):
        frames = arc(2.0, back_px=300.0)                      # 远投类：离筐 300px 起手
        cands = shot_candidates(frames, DEFAULT_CONFIG)
        self.assertIsNotNone(cands)
        self.assertEqual(cands["count"], 1)
        c = cands["candidates"][0]
        self.assertGreaterEqual(c["rise_px"], 25)
        self.assertIn(c["kind"], ("arc", "rise_only"))
        self.assertEqual(cands["review_offset_s"], 1.0)
        # 只提示：不带任何"进没进/计分"字段
        for key in ("made", "result", "points"):
            self.assertNotIn(key, c)

    def test_near_rim_start_is_classified_near(self):
        far = shot_candidates(arc(2.0, back_px=300.0), DEFAULT_CONFIG)
        near = shot_candidates(arc(2.0, back_px=60.0, y_start=110.0), DEFAULT_CONFIG)
        self.assertEqual(far["candidates"][0]["klass"], "far")
        self.assertEqual(near["candidates"][0]["klass"], "near")

    def test_flat_motion_is_not_a_candidate(self):
        rows = [frame(i / 30, (200.0 + i, 300.0, 12.0), rim=RIM) for i in range(40)]
        self.assertEqual(shot_candidates(rows, DEFAULT_CONFIG)["count"], 0,
                         "水平移动没有顶点，不该算候选")

    def test_arc_far_from_rim_is_not_a_candidate(self):
        rows = [frame(i / 30, (500.0, 300.0 - (120.0 * (i / 6) if i <= 6 else 120.0 * (2 - i / 6)),
                               12.0), rim=RIM)
                for i in range(13)]
        self.assertEqual(shot_candidates(rows, DEFAULT_CONFIG)["count"], 0,
                         "轨迹从没接近过筐，不该算候选")

    def test_old_trace_without_ball_returns_none(self):
        rows = [{"frame": 0, "t": 0.0, "cut": False, "persons": []}]
        self.assertIsNone(shot_candidates(rows, DEFAULT_CONFIG))
        self.assertIsNone(ball_visibility(rows, 30.0))

    def test_cut_breaks_candidate(self):
        """切镜标记来自"当前帧 vs 上一帧"的突变 → **cut 帧是新镜头首帧**。

        故意把新镜头首帧（cut 帧）的球坐标放到画面右上、且比旧镜头的最后一帧高很多：
        如果不按 cut 断段，窗口会把两帧拼成一条"上升 330px"的假弧线。
        """
        rows = []
        # 旧镜头：球在左下、缓慢向下（没有上升）
        for i in range(10):
            rows.append(frame(i / 30, (300.0 + i, 400.0 + i, 12.0), rim=RIM))
        # 新镜头首帧（cut 帧）：坐标跳到右上、靠近筐
        cut = frame(10 / 30, (990.0, 70.0, 12.0), rim=RIM)
        cut["cut"] = True
        rows.append(cut)
        # 新镜头后续：缓慢向下
        for i in range(1, 10):
            rows.append(frame((10 + i) / 30, (990.0, 70.0 + i, 12.0), rim=RIM))
        self.assertEqual(shot_candidates(rows, DEFAULT_CONFIG)["count"], 0,
                         "跨切镜的坐标跳变不该被当成一次出手")
        # 反向验证：把切镜/分段信息抹掉，这条假弧线**应该**出现 ——
        # 否则说明这个用例根本没有落到断段逻辑上（"为了让测试通过"的假通过）。
        noflag = []
        for r in rows:
            c = dict(r)
            c.pop("cut", None)
            noflag.append(c)
        self.assertEqual(shot_candidates(noflag, DEFAULT_CONFIG)["count"], 1,
                         "抹掉切镜标记后应当能拼出假候选（证明该用例确实在测断段）")

    def test_segment_change_breaks_candidate(self):
        rows = []
        for i in range(10):
            rows.append(frame(i / 30, (300.0 + i, 400.0 + i, 12.0), rim=RIM))
        for i in range(1, 10):
            r = frame((10 + i) / 30, (990.0, 70.0 + i, 12.0), rim=RIM)
            r["evidence_segment"] = 2                     # 跟踪段变了 → 必须断段
            rows.append(r)
        self.assertEqual(shot_candidates(rows, DEFAULT_CONFIG)["count"], 0)
        noflag = []
        for r in rows:
            c = dict(r)
            c.pop("evidence_segment", None)
            noflag.append(c)
        self.assertEqual(shot_candidates(noflag, DEFAULT_CONFIG)["count"], 1,
                         "抹掉分段信息后应当能拼出假候选（证明该用例确实在测断段）")

    def test_cross_segment_candidate_is_listed_separately(self):
        """被切镜从中间截断的弧线：主列表丢掉，但必须**单独列出来**（不能假装没看见）。

        造法：上升段全在旧镜头里，cut 帧本身就是顶点（所以按段限制口径，顶点的"上升段"
        只剩它自己 → 上升 0px → 被判不是出手）；随后新镜头里球下落。
        这正是实测里 night 14.014s 那条的形状。
        """
        rows = []
        for i in range(6):                      # 旧镜头：球持续上升，y 300→200
            rows.append(frame(i / 30, (900.0 + i * 4, 300.0 - i * 20, 12.0), rim=RIM))
        cut = frame(6 / 30, (960.0, 80.0, 12.0), rim=RIM)   # cut 帧 = 新镜头首帧 = 顶点
        cut["cut"] = True
        rows.append(cut)
        for i in range(1, 6):                   # 新镜头：下落
            rows.append(frame((6 + i) / 30, (960.0, 80.0 + i * 25, 12.0), rim=RIM))

        sc = shot_candidates(rows, DEFAULT_CONFIG)
        self.assertEqual(sc["count"], 0, "跨切镜的弧线不该进主列表（不跨段拼接）")
        self.assertEqual(sc["unmatched_count"], 0)
        self.assertEqual(sc["cross_segment_count"], 1, "丢了不等于不存在：必须单独列出")
        c = sc["cross_segment"][0]
        self.assertTrue(c["cross_segment"])
        self.assertEqual(c["blocks_in_window"], 2)
        self.assertGreaterEqual(c["rise_px"], 25)      # 不受段限制时上升是完整的
        self.assertIn(c["kind"], ("arc", "rise_only"))
        for key in ("made", "result", "points"):
            self.assertNotIn(key, c, "跨段候选同样只提示、不计分")

        # 反向对照：把切镜标记抹掉 → 这条弧线回到主列表，跨段列表变空。
        # 否则说明这个用例根本没走到断段逻辑上（"为了让测试通过"的假通过）。
        noflag = []
        for r in rows:
            c2 = dict(r)
            c2.pop("cut", None)
            noflag.append(c2)
        happy = shot_candidates(noflag, DEFAULT_CONFIG)
        self.assertEqual(happy["count"], 1, "没有切镜时它应当是一条普通候选")
        self.assertEqual(happy["cross_segment_count"], 0)

    def test_cross_segment_candidate_links_to_existing_event(self):
        """跨段候选也要走同一套事件关联（已识别为出手的就不必当成"漏检"）。"""
        rows = []
        for i in range(6):
            rows.append(frame(i / 30, (900.0 + i * 4, 300.0 - i * 20, 12.0), rim=RIM))
        cut = frame(6 / 30, (960.0, 80.0, 12.0), rim=RIM)
        cut["cut"] = True
        rows.append(cut)
        for i in range(1, 6):
            rows.append(frame((6 + i) / 30, (960.0, 80.0 + i * 25, 12.0), rim=RIM))
        ev = [{"t": 4.6, "release_t": 0.1, "type": "make"}]
        sc = shot_candidates(rows, DEFAULT_CONFIG, ev)
        c = sc["cross_segment"][0]
        self.assertTrue(c["matched"])
        self.assertEqual(c["matched_event_type"], "make")
        self.assertLessEqual(c["matched_delta_s"], 1.5)

    def test_evidence_flags_describe_ball_box_shape_and_do_not_filter(self):
        """球框被框成"竖长框"（多半框到了人）时，候选要**照旧列出**，只加证据提示。

        用户实测：夜间"球框"会框到球员头部。这里的口径是**描述证据**，不是判投篮 ——
        真实投篮的窗口里也会出现竖长框，所以绝不能拿它过滤候选。
        """
        rows = []
        for r in arc(2.0, back_px=300.0):
            box = r["ball"]["xyxy"]
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            r["ball"]["xyxy"] = [cx - 6, cy - 18, cx + 6, cy + 18]     # 12x36：h/w = 3
            rows.append(r)
        sc = shot_candidates(rows, DEFAULT_CONFIG)
        self.assertEqual(sc["count"], 1, "有证据提示的候选仍然要列出来（不许被过滤掉）")
        c = sc["candidates"][0]
        self.assertIn("ball_box_shape", c["evidence_flags"])
        self.assertEqual(c["tall_box_frac"], 1.0)
        self.assertEqual(c["ball_box_w_med"], 12.0)
        self.assertIn("ball_box_shape", sc["evidence_flag_counts"])

    def test_ball_stays_with_person_is_flagged_as_evidence(self):
        """球心全程落在人体框内（持球/走动）→ 提示 ball_with_person，且"连续在人框外"为 0。

        用户目视实测：场边人员抱球走动会产生候选（night 24.458 / 27.995）。
        """
        person = [0.0, 0.0, 2000.0, 1000.0]          # 整个人体框把球路全包住
        rows = []
        for r in arc(2.0, back_px=300.0):
            r["persons"] = [{"cls": "person", "conf": .9, "xyxy": person, "track_id": "P1"}]
            rows.append(r)
        sc = shot_candidates(rows, DEFAULT_CONFIG)
        self.assertEqual(sc["count"], 1, "证据提示不是过滤器：候选照旧列出")
        c = sc["candidates"][0]
        self.assertIn("ball_with_person", c["evidence_flags"])
        self.assertEqual(c["in_person_frac"], 1.0)
        self.assertEqual(c["outside_person_run"], 0, "全程贴着人 → 连续在人框外 0 帧")
        self.assertEqual(c["ball_box_w_ratio"], 1.0, "球框宽度正常 → 与全片中位宽比值为 1")

    def test_person_evidence_is_unknown_without_person_detection(self):
        """没有球员检测时必须是"未知"，不能当成"球不在人身上"。"""
        sc = shot_candidates(arc(2.0, back_px=300.0), DEFAULT_CONFIG)   # frame() 默认 persons=[]
        c = sc["candidates"][0]
        self.assertIsNone(c["in_person_frac"])
        self.assertIsNone(c["outside_person_run"])
        self.assertNotIn("ball_with_person", c["evidence_flags"])

    def test_cut_right_after_evidence_is_flagged(self):
        """观测刚结束画面就切走 → 标 cut_truncated（结果要去下一个镜头找）。

        用户要求把"切镜截断"和"球被遮挡/飞出画面"分开说：前者是画面换了、看不到结果，
        不能因为本段看不到就说没进。实测 day 的 4.004s / 18.819s 就是这种（差 0.33 / 0.13 秒）。
        """
        rows = arc(2.0, back_px=300.0)
        last = rows[-1]["t"]
        cut = frame(last + 0.2, None, rim=RIM)      # 切镜帧：新镜头首帧，本帧没有球
        cut["cut"] = True
        sc = shot_candidates(rows + [cut], DEFAULT_CONFIG)
        self.assertEqual(sc["count"], 1, "切镜不该把候选本身弄没")
        c = sc["candidates"][0]
        self.assertTrue(c["cut_truncated"])
        self.assertIn("cut_truncated", c["evidence_flags"])
        # 反向对照：切镜离得远（1.5s 后）说明球是别的原因消失的，不该标成截断
        far = frame(last + 1.5, None, rim=RIM)
        far["cut"] = True
        sc2 = shot_candidates(rows + [far], DEFAULT_CONFIG)
        self.assertFalse(sc2["candidates"][0]["cut_truncated"])
        self.assertNotIn("cut_truncated", sc2["candidates"][0]["evidence_flags"])

    def test_candidate_links_to_existing_event(self):
        frames = arc(2.0, back_px=300.0)
        ev = [{"t": 4.6, "release_t": 2.4, "type": "make"}]       # 离手 2.4s，判定收尾 4.6s
        sc = shot_candidates(frames, DEFAULT_CONFIG, ev)
        c = sc["candidates"][0]
        self.assertTrue(c["matched"])
        self.assertEqual(c["matched_event_type"], "make")
        self.assertLessEqual(c["matched_delta_s"], 1.5)
        self.assertEqual(sc["unmatched_count"], 0)

    def test_candidate_without_matching_event_is_flagged(self):
        frames = arc(2.0, back_px=300.0)
        ev = [{"t": 30.0, "release_t": 28.0, "type": "make"}]     # 离得很远
        sc = shot_candidates(frames, DEFAULT_CONFIG, ev)
        c = sc["candidates"][0]
        self.assertFalse(c["matched"])
        self.assertIsNone(c["matched_event_t"])
        self.assertEqual(sc["unmatched_count"], 1)
        self.assertIn("不计入出手次数", sc["note"])

    def test_ball_visibility_reports_blind_ranges(self):
        rows = [frame(i / 30, (200.0, 300.0, 12.0)) for i in range(30)]        # 0~1s 有球
        rows += [frame(t / 30, None) for t in range(30, 160)]                  # 1~5.3s 无球
        rows += [frame(t / 30, (200.0, 300.0, 12.0)) for t in range(160, 190)]
        bv = ball_visibility(rows, 30.0)
        self.assertEqual(bv["sampled_frames"], 190)
        self.assertEqual(bv["observed_frames"], 60)
        self.assertEqual(len(bv["blind_ranges"]), 1)
        self.assertGreater(bv["longest_blind_s"], 4.0)
        self.assertIn("不等于没有投篮", bv["note"])

    def test_candidates_do_not_change_engine_events(self):
        """同一份输入，候选层存在与否都不影响引擎事件（结构上也不共用代码路径）。"""
        frames = arc(2.0, back_px=300.0)
        s = LegacyShotStream(30, 1920)
        for r in frames:
            s.update(r["frame"], r["t"], [], [])
        d = s.to_dict()
        self.assertIn("shot_candidates", d)
        self.assertIn("ball_visibility", d["rim_tracking"])
        # events 里没有任何候选记录
        self.assertTrue(all("candidate" not in e for e in d["events"]))


if __name__ == "__main__":
    unittest.main()
