"""Shared detector coverage check; coverage is not labelled recall or accuracy."""
import os
from pathlib import Path

def coverage_verdict(hits, samples, error=None):
    rate = hits / samples if samples else None
    ok = None if error or not samples else rate >= .4
    return {'ok':ok,'ball_detection_rate':rate,'hit_frames':hits,'sampled_frames':samples,
            'basis':'detector_frame_coverage','error':error,
            'note': ('未完成检测器体检，不能判定素材不可用' if ok is None else
                     '可用于自动球检测；投篮判定和场地标定需分别验证' if ok else
                     '当前检测器覆盖不足，建议检查模型或画面；不代表所有算法均不可用')}

def pick_ball_weights(explicit=''):
    if explicit:return Path(explicit)
    root=Path(__file__).resolve().parents[2]
    candidates=[root/'runs/detect/ball/weights/best.pt',root/'data/shot_tracker_best.pt']
    if os.environ.get('AIHOOP_MODELS'):
        candidates.append(Path(os.environ['AIHOOP_MODELS'])/'rim_ball_v6.pt')
    return next((p for p in candidates if p.exists()),None)

def probe_detector(video, weights='', samples=24, imgsz=1280, device='cpu', max_seconds=0):
    model_path=pick_ball_weights(weights)
    if not model_path or not model_path.exists():
        return coverage_verdict(0,0,'未提供可用篮球模型')
    import cv2
    cap=cv2.VideoCapture(str(video))
    hits=seen=0
    try:
        if not cap.isOpened():raise ValueError('打不开视频')
        from ultralytics import YOLO
        model=YOLO(str(model_path))
        n=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps=cap.get(cv2.CAP_PROP_FPS) or 30
        if max_seconds:n=min(n,int(max_seconds*fps))
        if samples<1:raise ValueError('samples 必须为正数')
        for i in range(min(samples,n)):
            cap.set(cv2.CAP_PROP_POS_FRAMES,int(i*n/min(samples,n)))
            ok,frame=cap.read()
            if not ok:continue
            result=model.predict(frame,conf=.25,imgsz=imgsz,device=device,verbose=False)[0]
            seen+=1
            if result.boxes is not None and any('ball' in str(result.names[int(b.cls[0])]).lower() for b in result.boxes):
                hits+=1
        return coverage_verdict(hits,seen)
    except Exception as exc:
        return coverage_verdict(hits,seen,str(exc))
    finally:
        cap.release()
