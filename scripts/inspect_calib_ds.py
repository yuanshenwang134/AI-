"""体检标定数据集：看下载来的东西能不能直接用来训自动球场标定。

为什么要先体检：
  `camera-calibration-challenge` 的 Kaggle 数据集给的是**图像 + 相机标定参数**
  （728 张），**不是球场关键点标注**。它的官方路线是
  「训练球场线分割模型 → 从线拟合标定」，代码量比关键点路线大得多。
  而我这边的自动标定只需要**球场关键点**（有了关键点就能直接解单应矩阵），
  所以更省事的是「关键点模型」路线。

本脚本扫一遍数据目录，告诉你拿到了什么、能走哪条路、还缺什么。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_DIRS = [
    ROOT / "data" / "calib_ds",
    ROOT / "third_party" / "camera-calibration-challenge"
    / "basketball-instants-dataset",
    ROOT / "third_party" / "camera-calibration-challenge" / "data",
]


def scan(d: Path):
    if not d.exists():
        return None
    imgs = [p for p in d.rglob("*")
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    jsons = [p for p in d.rglob("*.json")]
    txts = [p for p in d.rglob("*.txt")]
    return {"dir": str(d), "images": len(imgs), "json": len(jsons),
            "txt": len(txts), "sample_json": jsons[:2], "sample_img": imgs[:2]}


def main() -> int:
    print("=== 扫描可能的标定数据目录 ===")
    found = []
    for d in CANDIDATE_DIRS:
        r = scan(d)
        if r:
            print(f"  {r['dir']}")
            print(f"     图像 {r['images']}   json {r['json']}   txt {r['txt']}")
            found.append(r)
        else:
            print(f"  {d} —— 不存在")
    if not found or all(r["images"] == 0 for r in found):
        print("\n✗ 还没找到数据。请按下面任一条路下载：")
        print("""
【路线 1｜关键点（推荐，我这边最省事）】
  来源：Roboflow Universe 的篮球场关键点数据集（YOLO-pose 格式，可直接训练）
  搜索关键词：basketball court keypoint detection
  下载：在数据集页面选 "YOLOv8" 格式 → 复制它给的下载代码片段
        （形如 pip install roboflow; from roboflow import Roboflow ...）
  放到：data/calib_ds/         （解压后应有 images/ 与 labels/）

【路线 2｜Kaggle 标定参数（官方数据集，但路线更重）】
  页面：https://www.kaggle.com/datasets/deepsportradar/basketball-instants-dataset
  方式 A（网页）：页面右侧 Download → 解压到
        third_party/camera-calibration-challenge/basketball-instants-dataset/
  方式 B（命令行，需要 Kaggle API token）：
        .venv\\Scripts\\pip.exe install kaggle
        # 把 Kaggle 的 kaggle.json 放到 %USERPROFILE%\\.kaggle\\
        .venv\\Scripts\\kaggle.exe datasets download deepsportradar/basketball-instants-dataset
        # 解压到 third_party\\camera-calibration-challenge\\basketball-instants-dataset\\
""")
        return 1

    print("\n=== 各目录内容判断 ===")
    for r in found:
        if r["images"] == 0:
            continue
        print(f"\n  {r['dir']}")
        if r["txt"] > r["images"] * 0.5:
            print("    → 有大量 txt：像是 **YOLO 关键点标注**（每图一个 txt）")
            print("       ✅ 可直接训关键点模型（我这边最省事）")
        if r["json"]:
            print(f"    → 有 json {r['json']} 个，看看里面是什么：")
            for p in r["sample_json"]:
                try:
                    d = json.loads(Path(p).read_text(encoding="utf-8"))
                    keys = list(d)[:8] if isinstance(d, dict) else f"list[{len(d)}]"
                    print(f"       {Path(p).name}: {keys}")
                except Exception as e:  # noqa: BLE001
                    print(f"       {Path(p).name}: 解析失败 {e}")
            print("       若含 camera_matrix / calibration / R,t 之类 → 是【标定参数】数据集，"
                  "官方路线是分割球场线再拟合（重）")
            print("       若含 keypoints / categories(keypoints) → 是【关键点】数据集（可直接用）")
        if r["sample_img"]:
            print(f"    样例图：{Path(r['sample_img'][0]).name}")
    print("\n把这段输出发我，我就知道该走哪条路、要不要写适配。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
