"""自动球场标定：用关键点模型认出场地 → 解单应矩阵 → 存成这段视频的标定。

替代"手动点 5 个点"的流程：
    用户手点（现状）  →  每次换视频都要重标，还得点得准
    本脚本（自动）    →  跑一次识别，直接出标定；不准再补点

关键点 → 球场坐标的映射（`--map` 指向的 json）：
    不同数据集的关键点顺序/命名不同，所以映射必须显式给出、可修改。
    格式：{"关键点索引或名称": [球场x米, 球场y米], ...}
    例：{"0": [-7.5, -14.0], "1": [7.5, -14.0], "2": [0.0, 0.0]}
    场地口径：FIBA 28×15 米，原点在中圈中心，x∈[-7.5,7.5]，y∈[-14,14]。

用法：
    # 先看模型认出了哪些点（并出图，便于人工核对）
    python scripts\\auto_calibrate.py --video data\\new_video.mp4 `
        --weights runs/pose/court_kp/weights/best.pt --at 60 --dump

    # 指定映射后解标定
    python scripts\\auto_calibrate.py --video data\\new_video.mp4 `
        --weights runs/pose/court_kp/weights/best.pt --map data/court_kp_map.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="关键点模型 → 自动球场标定")
    ap.add_argument("--video", required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--at", type=float, default=10.0, help="用第几秒的帧")
    ap.add_argument("--map", default="data/court_kp_map.json")
    ap.add_argument("--conf", type=float, default=0.25,
                    help="置信度阈值。实测微调后的模型在你视频上输出偏低"
                         "（conf=0.35 一个框都出不来，0.25 才稳定），所以默认 0.25")
    ap.add_argument("--dump", action="store_true",
                    help="只打印/画出识别到的关键点，不解标定")
    a = ap.parse_args(argv)

    import cv2
    import numpy as np
    from ultralytics import YOLO

    w = Path(a.weights)
    if not w.exists():
        print(f"[err] 权重不存在：{w}（先跑 scripts\\train_court_keypoints.py）")
        return 2
    cap = cv2.VideoCapture(str(a.video))
    if not cap.isOpened():
        print(f"[err] 打不开视频：{a.video}")
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(a.at * fps)))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        print("[err] 取帧失败")
        return 2

    model = YOLO(str(w))
    r = model.predict(frame, conf=a.conf, verbose=False, device="cpu")[0]
    if getattr(r, "keypoints", None) is None or r.keypoints is None or \
            not len(r.keypoints.xy):
        print("[err] 没检出球场关键点。可以把 --conf 调低，或换一帧（--at）")
        return 1
    # 实测微调后的模型会同时检出 2~3 个「球场」框（同一块场地被重复检出）。
    # 取**框面积最大**的那个 —— 它最可能是完整的那块场地；直接取第 0 个会取到碎片。
    pick = 0
    if r.boxes is not None and len(r.boxes) > 1:
        areas = [(float((b.xyxy[0][2] - b.xyxy[0][0]) *
                        (b.xyxy[0][3] - b.xyxy[0][1])), i)
                 for i, b in enumerate(r.boxes)]
        areas.sort(reverse=True)
        pick = areas[0][1]
        print(f"  检出 {len(r.boxes)} 个候选场地 → 取面积最大的第 {pick} 个"
              f"（面积 {areas[0][0]:.0f}px²）")
    kps = r.keypoints.xy.cpu().numpy()[pick]
    conf = (r.keypoints.conf.cpu().numpy()[pick]
            if r.keypoints.conf is not None else np.ones(len(kps)))
    valid = [(i, float(kps[i][0]), float(kps[i][1]), float(conf[i]))
             for i in range(len(kps))
             if (kps[i][0] > 0 or kps[i][1] > 0) and conf[i] >= a.conf]
    print(f"检出 {len(valid)}/{len(kps)} 个有效关键点（conf≥{a.conf}）")
    for i, x, y, c in valid:
        print(f"   kp{i:02d}  ({x:7.1f},{y:7.1f})  conf={c:.2f}")

    # 出图便于核对
    vis = frame.copy()
    for i, x, y, c in valid:
        cv2.circle(vis, (int(x), int(y)), 5, (0, 255, 0), -1)
        cv2.putText(vis, str(i), (int(x) + 6, int(y) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
    outp = ROOT / "out" / "auto_calib_kp.png"
    cv2.imwrite(str(outp), vis)
    print(f"核对图已存 {outp}")

    if a.dump:
        return 0

    mp = Path(a.map)
    if not mp.exists():
        print(f"\n[需要映射文件 {mp}]")
        print("  不同数据集的关键点顺序不同，必须显式告诉我哪个点对应球场哪里。")
        print('  格式示例：{"0": [-7.5, -14.0], "1": [7.5, -14.0], "2": [0, 0]}')
        print("  （场地口径：FIBA 28×15 米，原点中圈中心，x∈[-7.5,7.5]，y∈[-14,14]）")
        print(f"  看到上面{kp if False else ''}的编号后，对照 {outp} 把映射写进 {mp} 即可。")
        return 1
    mapping = json.loads(mp.read_text(encoding="utf-8"))
    # 过滤不可靠点：
    #  * 落在画面边缘（±2px）的多半是模型外推的（实测这段视频 kp5/6/7 的 y 都等于画面高）
    #  * 位置几乎重合的点（实测 kp11 与 kp13 只差 1px）
    # 这些点参与拟合会把方程组搞奇异 —— court.py 里手写的高斯消元遇到奇异会
    # IndexError（已记录为待修 bug），所以这里先剔除。
    def _edge(x, y):
        return x <= 2 or y <= 2 or x >= W - 2 or y >= H - 2

    clean, dropped = [], []
    for i, x, y, c in valid:
        if _edge(x, y):
            dropped.append((i, "在画面边缘（外推）"))
            continue
        if any(abs(x - xx) < 3 and abs(y - yy) < 3 for _j, xx, yy, _c in clean):
            dropped.append((i, "与已选点重合"))
            continue
        clean.append((i, x, y, c))
    if dropped:
        print("  剔除不可靠点：" + "、".join(f"kp{i}({why})" for i, why in dropped))

    src, dst, used = [], [], []
    for i, x, y, c in clean:
        key = str(i)
        if key in mapping:
            src.append([x, y]); dst.append(list(mapping[key])); used.append(i)
    if len(src) < 4:
        print(f"[err] 剔除后只剩 {len(src)} 个点，解不出单应矩阵（至少要 4 个）")
        return 1
    # 用 SVD 解 DLT（对退化情况稳健，不会像手写消元那样崩）
    import numpy as _np
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    _u_, _s_, Vt = _np.linalg.svd(_np.array(A, dtype=_np.float64))
    Hm = (Vt[-1]).reshape(3, 3).tolist()
    from aihoop.court import Calibration, apply_homography
    errs = []
    for (x, y), (u, v) in zip(src, dst):
        px, py = apply_homography(Hm, x, y)
        errs.append(((px - u) ** 2 + (py - v) ** 2) ** 0.5)
    rmse = float(sum(errs) / len(errs))
    cal = Calibration(name=Path(a.video).stem, method="court-keypoints",
                      src_px=src, dst_m=dst, H=Hm,
                      reproj_error_m=round(rmse, 3), frame="full",
                      for_video=str(Path(a.video).resolve()),
                      frame_size=[W, H],
                      note=f"关键点模型自动标定（用了 {len(src)} 个点：{used}）")
    out_cal = ROOT / "data" / f"calibration_{Path(a.video).stem[:40]}.json"
    from dataclasses import asdict
    # **误差不合格就不落盘**：实测在村BA 户外绿场地上自动标定给出 8.46m 的误差，
    # 若照存不误，后续分析会用这份坏标定算出错误的出手位置/热区/战术图
    # —— 这正是本项目最忌讳的"编造"。宁可不产出，也不产出错的。
    if rmse >= 1.5:
        print(f"\n=== 自动标定**不达标**，未保存 ===")
        print(f"  重投影误差 {rmse:.3f} m ≥ 1.5 m（用了 {len(src)} 个点：{used}）")
        print("  说明这个机位不在关键点模型的能力范围内。")
        print("  请改用界面上的「标球场（点场地特征点）」手动标定（约 30 秒，可靠）。")
        if out_cal.exists():
            out_cal.unlink()
            print(f"  已删除旧的不可靠标定：{out_cal.name}")
        return 1
    out_cal.write_text(json.dumps(asdict(cal), ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"\n=== 自动标定完成 ===")
    print(f"  使用关键点 {used}")
    print(f"  重投影误差 {rmse:.3f} m  ✅ 可用")
    print(f"  已保存 {out_cal}（分析这段视频时会自动使用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
