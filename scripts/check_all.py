"""一键跑完所有自检 —— 答辩前每次改动后跑这个。

包含：
  1. Python 端到端自检（tests/test_plan_a.py，11 项，零第三方依赖）
  1b.战术层自检（tests/test_plan_b.py，21 项，零第三方依赖）
  2. 实施手册的 Python 代码块可编译性（scripts/verify_doc_code.py）
  3. 实施手册引用的文件/函数是否存在（scripts/verify_doc_refs.py）
  4. 实施手册里的 CLI 调用是否与 argparse 一致（scripts/verify_doc_cli.py）
  5. 前端口径与功能自检（node web/_validate.js、web/_test.js，有 node 才跑）
  6. API 集成测试（tests/test_api.py，装了 fastapi 才跑）

用法：
    python scripts/check_all.py
    python scripts/check_all.py --quick     # 跳过需要第三方依赖的项

Windows 上如果 PowerShell 的执行策略拦住了 .ps1，用这个 .py 入口最省事。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
ENV = {
    "PYTHONPATH": os.pathsep.join([str(ROOT / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]),
    # 关键：子进程强制 UTF-8 输出。否则 Windows 上子进程按 GBK 写管道，
    # 中文会变成乱码，甚至产生无法回写的替换字符（UnicodeEncodeError）。
    "PYTHONIOENCODING": "utf-8",
}


def _safe_print(text: str) -> None:
    """按 UTF-8 直接写字节输出，绕开 Windows 控制台/管道代码页的限制。

    Windows 上 sys.stdout 可能是 GBK，遇到无法编码的字符（如 U+FFFD）
    会抛 UnicodeEncodeError 把整个脚本打断 —— 这里显式用 UTF-8 写 buffer，
    并在失败时退回 ASCII 以防彻底崩掉。
    """
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:
        print(text)
        return
    try:
        buf.write((text + "\n").encode("utf-8", errors="replace"))
        buf.flush()
    except Exception:  # noqa: BLE001
        print(text.encode("ascii", errors="replace").decode("ascii"))


def run(label: str, cmd: list[str], required: bool = True) -> bool:
    _safe_print(f"\n{'=' * 68}\n{label}\n{'=' * 68}")
    try:
        import os
        env = dict(os.environ)
        env.update(ENV)
        p = subprocess.run(cmd, cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
    except Exception as e:  # noqa: BLE001
        _safe_print(f"  [ERR] 无法执行：{type(e).__name__}: {e}")
        return not required
    tail = (p.stdout or "").strip().splitlines()
    for line in tail[-14:]:
        _safe_print("  " + line)
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        if err:
            _safe_print("  --- stderr ---")
            for line in err[-8:]:
                _safe_print("  " + line)
        _safe_print(f"  [FAIL] 退出码 {p.returncode}")
        return False
    _safe_print("  [OK]")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="跳过需要第三方依赖的项（API 测试）")
    args = ap.parse_args(argv)

    results: list[tuple[str, bool]] = []
    results.append(("投篮可见性回归", run("投篮可见性与遮挡单元测试",
        [PY, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_shot*.py"], required=not args.quick)))
    results.append(("外部检测报告回归", run("外部检测报告单元测试",
        [PY, "-B", "tests/test_external_report.py"], required=not args.quick)))

    results.append(("穿筐判据回归（米制下限/筐口外推）", run(
        "穿筐判据单元测试 tests/test_cross_metric.py",
        [PY, "-B", "tests/test_cross_metric.py"], required=not args.quick)))

    # nybo 两个假进球（贴筐掠过被误判成进）的合成回归：原片已不在本机，
    # 用几何用例固化，见 eval/ground_truth/nybo回归说明.md
    results.append(("贴筐掠过不许判进（nybo 合成回归）", run(
        "贴筐掠过回归 tests/test_rim_graze_nybo.py",
        [PY, "-B", "tests/test_rim_graze_nybo.py"], required=not args.quick)))

    # 「球不是质点」的净空判据：球心偏移超过 (1-球直径/筐内径)×rx 时球体压住篮圈，
    # 不许判进 —— 真实假进球（nathan 3.40s：贴筐掠过被判成"穿过筐心"）的回归，
    # 见 docs/假进球_nathan3.4s_2026-09-26.md
    results.append(("球净空判据（假进球回归）", run(
        "球净空 + 筐位不确定度单元测试 tests/test_hoop_uncertainty.py",
        [PY, "-B", "tests/test_hoop_uncertainty.py"], required=not args.quick)))

    results.append(("Python 端到端自检（11 项）", run(
        "1/9 Python 端到端自检 tests/test_plan_a.py",
        [PY, "-B", "tests/test_plan_a.py"])))

    results.append(("比分牌 + 球/篮筐自检（60 项）", run(
        "2/9 比分牌识别 + 证据融合自检 tests/test_scoreboard.py",
        [PY, "-B", "tests/test_scoreboard.py"])))

    results.append(("篮下判进球自检（17 项）", run(
        "2b/9 篮下判进球（不靠比分牌/球检测器）自检 tests/test_hoopsight.py",
        [PY, "-B", "tests/test_hoopsight.py"])))

    results.append(("进球分队自检（16 项）", run(
        "2c/9 「这一球是哪一队进的」自检 tests/test_baskets.py",
        [PY, "-B", "tests/test_baskets.py"])))

    results.append(("战术层自检（21 项）", run(
        "3/9 战术层自检 tests/test_plan_b.py",
        [PY, "-B", "tests/test_plan_b.py"])))

    results.append(("手册代码块可编译", run(
        "4/9 手册 Python 代码块编译检查",
        [PY, "-B", "scripts/verify_doc_code.py"])))

    results.append(("手册引用有效", run(
        "5/9 手册引用的文件/函数对账",
        [PY, "-B", "scripts/verify_doc_refs.py"])))

    results.append(("手册 CLI 调用有效", run(
        "6/9 手册 CLI 调用 vs argparse",
        [PY, "-B", "scripts/verify_doc_cli.py"])))

    results.append(("PowerShell 脚本带 BOM", run(
        "7/9 .ps1 编码检查（PowerShell 5.1 兼容）",
        [PY, "-B", "scripts/fix_ps1_bom.py", "--check"])))

    node = shutil.which("node")
    if node:
        ok1 = run("8/9 前端功能自检 web/_test.js", [node, "web/_test.js"])
        ok2 = run("8/9 前端口径校验 web/_validate.js", [node, "web/_validate.js"])
        # 复核页「系统建议」的 7 项行为检查（unknown 状态、建议筛选、批量跳过未知、
        # 保存失败不本地改判…）—— 单独一个文件，很容易在门禁里漏掉，所以显式纳入。
        ok3 = run("8b/9 复核页建议行为检查 tests/test_review_suggestions.js",
                  [node, "tests/test_review_suggestions.js"])
        ok4 = run("8c/9 复核事件定位检查 tests/test_review_matching.js",
                  [node, "tests/test_review_matching.js"])
        # 「结果未知」的渲染口径：待确认不能显示成"未中"（徽章配色 + tooltip 文案 + 样式存在）
        ok5 = run("8d/9 未知结果渲染检查 tests/test_unknown_style.js",
                  [node, "tests/test_unknown_style.js"])
        # 上传页「开始分析」的请求体契约：没标定球场时必须带 allow_no_calibration，
        # 否则后端 JobCreate 默认 False 会直接把任务判失败（用户实测踩过：
        # "不标定球场他就是不给分析"，而引导里写着"不标也能分析"）。
        ok6 = run("8e/9 上传请求体契约 tests/test_upload_payload.js",
                  [node, "tests/test_upload_payload.js"])
        # 界面去重与首页：用户 2026-09-27 提的 5 条（标球场按钮重复、进球标注/随机种子
        # 不该在主流程、顶栏与侧栏导航重复、要有主界面、菜单指向不存在的页）
        ok7 = run("8f/9 界面去重与首页 tests/test_ui_home.js",
                  [node, "tests/test_ui_home.js"])
        # 只标篮筐中心时的接入：hoopsight 路径原来会因为 rx=0 的单样本篮筐被
        # `_visible_hoops` 拒绝而报 hoopsight_error（fixedcam 页面显示 1/4 的直接原因），
        # 现在只有 rx、ry 都为正才用人手筐，否则把中心当 hoop_hint 交给检测器。
        # 这条回归是队友侧加的，之前没接进门禁 —— 不接线就等于没有保护。
        ok8 = run("8g/9 只标中心的篮筐接入 tests/test_shot_center_hint.py",
                  [PY, "-B", "tests/test_shot_center_hint.py"])
        # 前端接线：API.videoUrl 曾被定义两次（互相覆盖）导致总览/高光页拿不到原视频；
        # 取帧类按钮也曾只判断后端状态、没判断"表单里有没有视频"，点了只弹个提示就
        # return，用户看到的是"没法取帧"。两条都用测试钉住。
        ok9 = run("8h/9 前端接线检查 tests/test_frontend_wiring.js",
                  [node, "tests/test_frontend_wiring.js"])
        results.append(("前端自检", ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7))
        results.append(("只标中心的篮筐接入（hoopsight）", ok8))
        results.append(("前端接线（videoUrl / 取帧前置条件）", ok9))
    else:
        _safe_print("\n[skip] 未找到 node，跳过前端自检（不影响后端功能）")

    if args.quick:
        _safe_print("\n[skip] --quick：跳过 API 集成测试")
    else:
        try:
            import fastapi  # noqa: F401
            import httpx    # noqa: F401
            results.append(("API 集成测试（3 项）", run(
                "9/9 API 集成测试 tests/test_api.py",
                [PY, "-B", "tests/test_api.py"])))
        except ImportError:
            _safe_print("\n[skip] 未安装 fastapi/httpx，跳过 API 测试"
                        "（pip install -r requirements.txt 后可跑）")

    _safe_print(f"\n{'=' * 68}\n汇总\n{'=' * 68}")
    for name, ok in results:
        _safe_print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}")
    bad = sum(1 for _, ok in results if not ok)
    _safe_print(f"\n{'全部通过' if bad == 0 else f'{bad} 项失败'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
