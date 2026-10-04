"""位置未知（占位坐标）的出手，**任何导出都不能给假的距离/区域**。

背景：旧引擎在没有球场标定时，会把出手位置写成占位坐标 `(0, -1.575)`（贴着被进攻篮筐），
`rules.build_shots` 会给这类出手打 `location_unknown` 标签。曾经的问题是：
界面与 markdown 都说"位置未知、不给热区"，但

  * `export.write_shots_csv` / `shots_to_csv_string` 照抄 x/y 算出 `distance=0.0`、`zone=禁区`；
  * `report._hot_zones` 把它聚成"禁区 6 投 5 中 83.3%"，而前端报告页直接渲染 `report.json.hot_zones`。

于是用户下载的 CSV、报告页看到的"高效出手区域"全是假的。这个测试把三条导出路径一起钉住。
"""
import csv
import io
import tempfile
import unittest
from pathlib import Path

from aihoop.export import shots_to_csv_string, write_shots_csv
from aihoop.model import Shot
from aihoop.report import _hot_zones, _key_shots
from aihoop.rules import shot_chart


def shot(**kw) -> Shot:
    """默认造一个"命中但位置未知"的出手（旧引擎无标定时的典型情况）。"""
    base = dict(t=6.0, team="home", player_id="T1", x=0.0, y=-1.575, value=2,
                made=True, counts_for_score=True, result="made", period=1,
                outcome_source="legacy_ball_rim", confidence=0.8,
                tags=["legacy_shot_engine", "location_unknown"])
    base.update(kw)
    return Shot(**base)


def placed(**kw) -> Shot:
    """位置真实的出手（有球场坐标）。"""
    base = dict(x=3.2, y=4.5, tags=["legacy_shot_engine"])
    base.update(kw)
    return shot(**base)


def read_csv(path: Path) -> list[dict]:
    with io.open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


class ShotsCsvLocation(unittest.TestCase):
    def test_csv_writes_empty_distance_and_unknown_zone_for_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shots.csv"
            write_shots_csv(str(p), [shot(t=6.0)])
            rows = read_csv(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["distance"], "", "位置未知时距离必须导出为空，不能是 0.0")
        self.assertEqual(rows[0]["zone"], "位置未知")
        self.assertIn("location_unknown", rows[0]["tags"])
        # 原始占位坐标仍保留（方便复核"系统把它放在哪了"），但不带假的距离/区域
        self.assertEqual(rows[0]["x"], "0.0")
        self.assertEqual(rows[0]["y"], "-1.575")

    def test_csv_string_uses_the_same_rule(self):
        rows = list(csv.DictReader(io.StringIO(shots_to_csv_string([shot()]))))
        self.assertEqual(rows[0]["zone"], "位置未知")

    def test_known_location_keeps_distance_and_zone(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shots.csv"
            write_shots_csv(str(p), [placed()])
            rows = read_csv(p)
        self.assertNotEqual(rows[0]["distance"], "", "位置真实时必须给出距离")
        self.assertNotEqual(rows[0]["zone"], "位置未知")

    def test_hot_zones_skip_placeholder_shots(self):
        shots = [shot(t=1.0), shot(t=2.0), shot(t=3.0),          # 3 次位置未知
                 placed(t=4.0), placed(t=5.0), placed(t=6.0)]     # 3 次位置真实
        zones = _hot_zones(shots)
        self.assertEqual([z["zone"] for z in zones], [z["zone"] for z in zones if z["zone"] != "禁区"],
                         "位置未知的出手不能聚进热区")
        self.assertEqual(sum(z["att"] for z in zones), 3,
                         "热区只应统计位置真实的 3 次出手")

    def test_hot_zones_empty_when_all_unknown(self):
        self.assertEqual(_hot_zones([shot(t=1.0), shot(t=2.0), shot(t=3.0)]), [])

    def test_key_shots_keep_event_but_no_fake_zone(self):
        q4 = shot(t=100.0, period=4)                 # 末节进球、位置未知
        real = placed(t=110.0, period=4)             # 末节进球、位置真实
        keys = _key_shots([q4, real], {})
        self.assertEqual(len(keys), 2, "位置未知的关键球仍是真实事件，不能从列表里消失")
        by_t = {k["t"]: k for k in keys}
        self.assertEqual(by_t[100.0]["zone"], "位置未知")
        self.assertIsNone(by_t[100.0]["distance"])
        self.assertNotEqual(by_t[110.0]["zone"], "位置未知")
        self.assertIsInstance(by_t[110.0]["distance"], float)

    def test_shot_chart_still_excludes_placeholder(self):
        chart = shot_chart([shot(), placed()])
        self.assertEqual(len(chart["points"]), 1, "出手图只应包含位置真实的那一次")


if __name__ == "__main__":
    unittest.main()
