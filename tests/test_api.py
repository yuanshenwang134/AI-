"""API 集成测试 —— 用 FastAPI TestClient 走一遍真实 HTTP 流程。

覆盖：
  建任务 -> 轮询到完成 -> 读总览/球员/热区/战报/高光 -> 导出下载
  -> 人工复核改判 -> 改判后统计确实变化 -> 静态文件托管

需要 fastapi + httpx（requirements.txt 里已包含）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _client():
    from fastapi.testclient import TestClient
    from aihoop import api
    return TestClient(api.app), api


def _wait_done(c, job_id: str, timeout: float = 180.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200, r.text
        d = r.json()
        if d["status"] in ("done", "error"):
            return d
        time.sleep(0.3)
    raise AssertionError(f"任务超时未完成：{job_id}")


def test_api_full_flow():
    c, api = _client()

    # 健康检查：即使没装 ffmpeg / ultralytics 也必须能返回
    h = c.get("/api/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True

    # 建一个短一点的合成任务（跑得快）
    r = c.post("/api/jobs", json={"source": "synthetic", "seed": 3,
                                  "duration": 360.0,
                                  "make_highlights": False})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    d = _wait_done(c, job_id)
    assert d["status"] == "done", d.get("error")
    assert d["progress"] == 1.0
    assert "比分" in d["summary"]

    # --- 总览 ---
    g = c.get(f"/api/games/{job_id}").json()
    assert g["score"]["home"] + g["score"]["away"] > 0
    assert len(g["timeline"]) > 5
    assert sum(q["home"] for q in g["quarter_scores"]) == g["score"]["home"]

    # --- 球员统计 ---
    players = c.get(f"/api/games/{job_id}/players").json()
    assert players and "fg_pct" in players[0]
    assert sum(p["points"] for p in players) == \
        g["score"]["home"] + g["score"]["away"]

    # --- 热区 ---
    sc = c.get(f"/api/games/{job_id}/shotchart").json()
    assert sc["all"]["points"] and sc["all"]["zones"]
    assert "home" in sc["by_team"] and "away" in sc["by_team"]

    # --- 战报 ---
    rep = c.get(f"/api/games/{job_id}/report").json()
    assert "最终比分" in rep["markdown"]
    assert rep["json"]["final_score"] == g["score"]

    # --- 高光（未开 ffmpeg 也应有时间码列表）---
    hl = c.get(f"/api/games/{job_id}/highlights").json()
    assert isinstance(hl["clips"], list)
    # 接口必须**核对文件是否真的在磁盘上**：以前照抄 clips.json 的 available，
    # 片段被清理脚本删掉后仍报 available=true，前端拿 404 地址去播，
    # 用户只看到「文件缺失或编码不支持」而查不出原因。
    assert "available" in hl and "missing" in hl and "can_regenerate" in hl
    assert hl["available"] == sum(1 for x in hl["clips"] if x["available"])

    # --- 套餐 B：战术层接口 ---
    tac = c.get(f"/api/games/{job_id}/tactics")
    assert tac.status_code == 200, tac.text[:200]
    tj = tac.json()
    assert tj["available"] is True, tj.get("reason")
    assert tj["possession"]["total"] > 0
    assert tj["passes"]["total"] > 0
    assert any(v["nodes"] for v in tj["pass_network"].values())
    # game.json 里要有战术摘要（总览页的"战术"卡片读它）
    assert g["tactics"]["available"] is True
    # 逐帧俯视战术图
    fr = c.get(f"/api/games/{job_id}/tactics/frames").json()
    assert fr["available"] is True and fr["count"] > 0
    assert fr["frames"][0]["players"], fr["frames"][0]

    # --- 导出下载 ---
    for fmt, marker in (("csv_stats", "球员"), ("csv_shots", "zone"),
                        ("report_md", "最终比分"), ("json", "score"),
                        ("csv_passes", "传球"), ("csv_spacing", "空间"),
                        ("tactics", "pass_network")):
        r = c.get(f"/api/games/{job_id}/export", params={"fmt": fmt})
        assert r.status_code == 200, f"{fmt}: {r.text[:200]}"
        assert marker in r.text, f"{fmt} 内容不对"

    # 非法 fmt 要报 400
    assert c.get(f"/api/games/{job_id}/export",
                 params={"fmt": "bogus"}).status_code == 400


def test_api_review_and_correct():
    """人工复核改判：改判后比分/统计必须随之变化，且口径保持一致。"""
    c, api = _client()

    r = c.post("/api/jobs", json={"source": "synthetic", "seed": 5,
                                  "duration": 360.0,
                                  "make_highlights": False})
    job_id = r.json()["job_id"]
    d = _wait_done(c, job_id)
    assert d["status"] == "done", d.get("error")

    before = c.get(f"/api/games/{job_id}").json()
    total_before = before["score"]["home"] + before["score"]["away"]

    # 复核列表
    rv = c.get(f"/api/games/{job_id}/review")
    assert rv.status_code == 200
    assert "needs_review" in rv.json()

    # 挑一次「未中的两分」改判成「命中的三分」
    idx = next(i for i, s in enumerate(before["timeline"]) if not s["made"])

    r = c.post(f"/api/games/{job_id}/shots/{idx}/correct",
               json={"made": True, "value": 3})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    after = c.get(f"/api/games/{job_id}").json()
    total_after = after["score"]["home"] + after["score"]["away"]
    assert total_after > total_before, "改判成命中后总分必须增加"

    s = after["timeline"][idx]
    assert s["made"] is True and s["value"] == 3
    assert s["source"] == "manual", "改判的出手来源应标为 manual"

    # 统计口径自洽：球员得分之和 == 总分
    players = c.get(f"/api/games/{job_id}/players").json()
    assert sum(p["points"] for p in players) == total_after

    # 越界 index 应报 400
    assert c.post(f"/api/games/{job_id}/shots/99999/correct",
                  json={"made": True}).status_code == 400


def test_api_web_assets_and_errors():
    c, api = _client()
    # 不存在的任务 -> 404
    assert c.get("/api/jobs/nonexistent").status_code == 404
    assert c.get("/api/games/nonexistent").status_code == 404
    # 根路径是纯文本提示
    assert c.get("/").status_code == 200
    # 路径穿越必须被挡掉
    r = c.post("/api/jobs", json={"source": "synthetic", "seed": 1,
                                  "duration": 180.0,
                                  "make_highlights": False})
    jid = r.json()["job_id"]
    _wait_done(c, jid)
    bad = c.get(f"/api/media/{jid}/../../../../Windows/win.ini")
    assert bad.status_code in (403, 404), bad.status_code


def _run_all() -> int:
    import traceback
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    ok = fail = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            fail += 1
    print(f"\nAPI 测试：{ok} 通过 / {fail} 失败（共 {ok + fail} 项）")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
