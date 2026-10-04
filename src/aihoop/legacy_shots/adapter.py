"""旧事件转换为新版 Attempt；不导入旧应用、统计或估算三分。"""
import math

def to_attempts(events, rt, calibration=None, value=2, counts_for_score=True):
    from ..sources import Attempt
    result=[]
    for event in events:
        t=float(event.get('release_t',event['t']))
        pid=str(event.get('player_id') or '')
        player=rt.players.get(pid)
        known_team=bool(player and player.team in ('home','away'))
        team=player.team if known_team else 'home'  # storage bucket only, explicitly unknown
        kind=event['type'];made={'make':True,'miss':False,'uncertain':None}[kind]
        suggestion=event.get('suggested_made')
        evidence=('legacy_occlusion_descent' if suggestion is not None and event.get('review_reason') == 'occlusion_descent_suggestion' else
                  'legacy_occluded_entry' if suggestion is not None else
                  'legacy_cross_measured' if made is True else
                  'legacy_left_region_or_timeout' if made is False else
                  'legacy_insufficient_evidence')
        tags=[evidence,'legacy_shot_engine']
        if made is None:tags+=['outcome_unknown','needs_review']
        if suggestion is not None:tags+=['inferred_outcome','needs_review'];made=None
        if not known_team:tags+=['team_unknown','needs_review']
        a=Attempt(t=t,team=team,player_id=pid or 'UNASSIGNED',x=0.,y=-1.575,
            made=made,conf=.8 if made is not None else .4,source='legacy_ball_rim',
            release_frame=round(t*rt.fps),location_source='rim_placeholder',location_estimated=True,
            forced_value=value,value_assumed=True,value_source='default',
            counts_for_score=counts_for_score and known_team and made is not None,
            tags=list(dict.fromkeys(tags)),evidence=evidence,
            crossing_t=event.get('crossing_t',event.get('rim_crossing_t')),
            suggested_made=suggestion, review_t=event.get('review_t'), decision_t=float(event["t"]),
            release_source=event.get("release_source", ""),
            clip_end=min(rt.duration, float(event['t'])+1.5) if rt.duration>0 else float(event['t'])+1.5)
        # 只把出手人脚下投到地面；空中篮球不能当作地面出手点。
        xy=event.get('release_xy')
        if (calibration is not None and rt.detections_meta.get('calibration_valid')
                and event.get('release_source')=='player_feet' and xy):
            x,y=calibration.to_court(*xy)
            if math.isfinite(x) and math.isfinite(y) and abs(x)<=9 and abs(y)<=16:
                a.x,a.y=x,y;a.location_source='player_feet';a.location_estimated=False
                a.forced_value=None;a.value_assumed=False;a.value_source='court_geometry'
        result.append(a)
    return sorted(result,key=lambda a:a.t)
