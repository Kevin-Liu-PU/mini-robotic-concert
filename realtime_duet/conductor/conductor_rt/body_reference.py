"""Automatic, geometry-gated shoulder coordinates. No device or file I/O.

Face mesh has no calibrated per-point confidence. The quality below is a
conservative geometry/tracking heuristic, never a probability of correctness.
All transforms use the CURRENT shoulders: smoothing the camera-space origin
would turn camera/body translation into apparent hand motion.
"""
from dataclasses import replace
import math

FACE_POINTS = (1, 10, 33, 152, 234, 263, 454)


def pose_side_for_hand(hand, mirrored=True):
    """Convert the person's anatomical choice to this pipeline's pose indices.

    prepare_frame mirrors BEFORE Holistic. Pose Left/Right indices therefore
    name the reflected body; reverse them for the person's anatomical label.
    The preview uses these same reflected pixels, not a second mirror.
    """
    if hand not in ('left','right'):raise ValueError('conducting hand must be left or right')
    side=0 if hand=='left' else 1
    return 1-side if mirrored else side


def finite_point(p, dimensions=2):
    return p is not None and len(p) >= dimensions and all(math.isfinite(v) for v in p[:dimensions])


class BodyReference:
    acquire_s = .25
    hold_s = .25

    def __init__(self, aspect):
        self.aspect = aspect
        self.last = None
        self.last_good = None
        self.stable_since = None
        self.good_frames = 0
        self.chin = None
        self.last_chin = None
        self.smoothed = None
        self.status = 'acquiring'
        self.reason = 'no_reference'
        self.quality = 0.

    def invalidate(self, stamp, reason):
        self.gap_resumed = False
        self.status = ('held_no_events' if self.last_good is not None and
                       0 <= stamp-self.last_good <= self.hold_s else 'lost')
        self.reason, self.quality = reason, 0.
        self.stable_since = None
        self.good_frames = 0
        self.smoothed = None
        self.chin = self.last_chin = None
        return None

    def update(self, record, stamp):
        self.frame_gap_s = None
        self.gap_resumed = False
        pose, face = record.get('pose'), record.get('face')
        if not pose or len(pose) < 25 or not all(finite_point(pose[i], 3) and
                pose[i][2] >= .7 and .02 < pose[i][0] < .98 and .02 < pose[i][1] < .98 for i in (11, 12)):
            return self.invalidate(stamp, 'shoulders_missing')
        if not face or any(str(i) not in face or not finite_point(face[str(i)], 3) or
                not all(.015 < v < .985 for v in face[str(i)][:2]) for i in FACE_POINTS):
            return self.invalidate(stamp, 'face_missing_or_clipped')
        a, b = sorted(((pose[i][0]*self.aspect, pose[i][1]) for i in (11, 12)))
        dx, dy = b[0]-a[0], b[1]-a[1]
        span = math.hypot(dx, dy)
        if not .06 < span < .85 or abs(dy) > .45*span:
            return self.invalidate(stamp, 'shoulder_geometry')
        center = ((a[0]+b[0])/2, (a[1]+b[1])/2)
        ux, uy = dx/span, dy/span

        def relative(p):
            x, y = p[0]*self.aspect-center[0], p[1]-center[1]
            return [(x*ux+y*uy)/span, (-x*uy+y*ux)/span]

        hips = all(finite_point(pose[i], 3) and pose[i][2] >= .65 for i in (23, 24))
        if hips:
            hip = relative([(pose[23][0]+pose[24][0])/2, (pose[23][1]+pose[24][1])/2])
            if not .35 < hip[1] < 2.8 or abs(hip[0]) > .65:
                return self.invalidate(stamp, 'torso_rotation_or_fold')
        f = {i: relative(face[str(i)]) for i in FACE_POINTS}
        left, right = sorted((f[33], f[263]))
        eye_width = right[0]-left[0]
        height = f[152][1]-f[10][1]
        nose_fraction = (f[1][0]-left[0])/max(eye_width, 1e-8)
        depth_asymmetry = abs(face['234'][2]-face['454'][2])*self.aspect/span
        if (not .15 < eye_width < .75 or not .3 < height < 1.5 or
                not .25 < nose_fraction < .75 or depth_asymmetry > .55*eye_width or
                abs(left[1]-right[1]) > .35*eye_width or
                not -.95 < f[152][1] < .10 or abs(f[152][0]) > .45 or
                not f[10][1] < f[1][1] < f[152][1]):
            return self.invalidate(stamp, 'face_geometry_or_head_turn')
        dt = .033 if self.last is None else stamp-self.last[0]
        if self.last is not None and 0 < dt < .3:
            _, old_center, old_span, old_u = self.last
            shifted = math.dist(center, old_center)/old_span
            scale_step = abs(math.log(span/old_span))
            rotated = abs(ux*old_u[1]-uy*old_u[0])
            if shifted > .16+4*dt or scale_step > .08+1.5*dt or rotated > .08+dt:
                self.last = (stamp, center, span, (ux, uy))
                return self.invalidate(stamp, 'reference_jump')
        self.last = (stamp, center, span, (ux, uy))
        self.frame_gap_s = dt
        # Keep a previously acquired reference only across a bounded compute
        # gap when this NEW frame passes all shoulder/face/motion geometry
        # checks. Missing/invalid observations still require full acquisition.
        # VisualDetector independently clears all hand-trajectory and gesture
        # evidence at >150 ms, so this cannot invent a crossing inside the gap.
        self.gap_resumed = (.15 < dt <= self.hold_s and self.status == 'tracked'
                            and self.last_good is not None and stamp-self.last_good <= self.hold_s)
        if dt <= 0 or (dt > .15 and not self.gap_resumed):
            self.invalidate(stamp, 'frame_gap')
        chin = f[152][1]
        if self.last_chin is not None and abs(chin-self.last_chin) > .06+1.5*max(dt, 0):
            return self.invalidate(stamp, 'chin_jump')
        self.last_chin = chin
        alpha = 1-math.exp(-max(.001, dt)/.045)
        self.chin = chin if self.chin is None else self.chin+alpha*(chin-self.chin)
        # Map body-relative units into a canonical image for the legacy gesture
        # extractor. A shoulder span is always .30; the camera aspect is removed.
        def canonical(p):
            x, y = relative(p)
            return [.5+.3*x, .55+.3*y]
        converted = dict(pose=[canonical(p)+[p[2]] for p in pose],
                         hands=[dict(label=h['label'], points=[canonical(p) for p in h['points']])
                                for h in record.get('hands', [])])
        # Smooth only body-relative wrist/hand motion, never the camera origin.
        if self.smoothed is not None:
            motion_alpha = 1-math.exp(-max(.001, dt)/.025)
            for i in (15, 16):
                converted['pose'][i][:2] = [o+motion_alpha*(n-o) for o,n in
                    zip(self.smoothed['pose'][i][:2], converted['pose'][i][:2])]
            old_hands = {h['label']: h for h in self.smoothed['hands']}
            for h in converted['hands']:
                if h['label'] in old_hands:
                    h['points'] = [[o+motion_alpha*(n-o) for o,n in zip(op,np)] for op,np in
                                   zip(old_hands[h['label']]['points'], h['points'])]
        self.smoothed = converted
        self.last_good = stamp
        if self.stable_since is None:
            self.stable_since = stamp
        self.good_frames += 1
        ready = stamp-self.stable_since >= self.acquire_s and self.good_frames >= 5
        self.status = 'tracked' if ready else 'acquiring'
        self.reason = 'shoulders_torso_face' if hips else 'shoulders_face_hips_unavailable'
        self.quality = (.85 if hips else .75) if ready else 0.
        return (converted, .55+.3*self.chin) if ready else None


class AutomaticVisualDetector:
    """Suppress all visual events across reference loss/reacquisition."""
    def __init__(self, aspect, conducting_side=1, wrist_fallback_s=.30, meter=4, settled_endpoints=True):
        if conducting_side not in (0,1):raise ValueError('a fixed conducting side is required')
        self.wrist_fallback_s=wrist_fallback_s
        # Existing 0.30 s event fallback remains unchanged. Up to the existing
        # 0.60 s Stop-approach horizon, preserve ONLY continuously observed,
        # reliable same-wrist motion. No events/tracking-ready while waiting.
        self.motion_hold_s=.60
        self.meter=meter
        self.settled_endpoints=settled_endpoints
        self.locked_side=conducting_side
        self.last_matched={0:None,1:None}
        self.reference = BodyReference(aspect)
        self.detector = None
        self.last_good = None
        self.selected_side = conducting_side
        self.control_state = "idle"
        self.prepare_since = {0:None,1:None}
        self.prepare_sent = {0:False,1:False}

    def update(self, record, stamp, received, source_s, frame, bpm):
        from .vision import VisualDetector, l_shape_conditions
        transformed = self.reference.update(record, stamp)
        pose=record.get('pose')
        wrists={str(i):round(pose[i][2],3) if pose and len(pose)>i and finite_point(pose[i],3) else None for i in (15,16)}
        status = dict(reference_state=self.reference.status, reference_reason=self.reference.reason,
                      reference_frame_gap_s=self.reference.frame_gap_s,
                      reference_gap_resumed=self.reference.gap_resumed,
                      reference_quality=self.reference.quality, reference_confidence_is_probability=False,
                      tracking_ready=False, wrist_visibility=wrists, detected_hands=len(record.get("hands",[])),
                      selected_side=self.selected_side, locked_side=self.locked_side,
                      prepare_hands={str(side):dict(eligible=False,conditions={},held_150ms=False,reason='reference_unavailable') for side in (0,1)})
        if transformed is None:
            self.prepare_since={0:None,1:None};self.prepare_sent={0:False,1:False}
            self.detector = None
            return dict(prepare=False, stop=False, observation=None, visible=False,
                        association='reference_unavailable', **status)
        body_record, chin = transformed
        # L chooses the conducting wrist. Once armed, preserve that anatomical
        # identity through short mesh gaps; never replace it with an idle hand.
        valid_hands=[converted for original,converted in zip(record.get('hands',[]),body_record['hands'])
                     if all(finite_point(point) and all(.015<v<.985 for v in point[:2]) for point in original['points'])]
        body_record=dict(body_record,hands=valid_hands)
        matched_hands={}
        confirmed=[]
        for side in (0,1):
            original=record['pose'][15+side]
            wrist=body_record['pose'][15+side][:2]
            hands=body_record['hands']
            nearest=min(hands,key=lambda h:math.dist(h['points'][0],wrist)) if hands else None
            eligible=(original[2]>=.65 and all(.015<v<.985 for v in original[:2]) and
                      nearest is not None and math.dist(nearest['points'][0],wrist)<.12)
            conditions=l_shape_conditions(nearest['points'],chin,1) if eligible else {}
            if eligible:
                matched_hands[side]=nearest
                self.last_matched[side]=stamp
            if eligible and all(conditions.values()):
                if self.prepare_since[side] is None:self.prepare_since[side]=stamp
            else:
                self.prepare_since[side]=None;self.prepare_sent[side]=False
            held=self.prepare_since[side] is not None and stamp-self.prepare_since[side]>=.15
            if held and not self.prepare_sent[side] and side==self.locked_side:
                confirmed.append(side);self.prepare_sent[side]=True
            status['prepare_hands'][str(side)]=dict(eligible=bool(eligible),conditions=conditions,
                held_150ms=bool(held),enabled=side==self.locked_side,
                reason='matched_hand' if eligible else 'wrist_or_hand_unavailable')
        prepare_side=self.locked_side if self.locked_side in confirmed else None
        # No hand switching, including L from the other hand or reacquisition.
        chosen=self.locked_side
        status['selected_side']=chosen
        status['locked_side']=self.locked_side
        selected_hand=matched_hands.get(chosen)
        last_mesh=self.last_matched[chosen]
        status['hand_mesh_age_ms']=None if last_mesh is None else 1000*(stamp-last_mesh)
        original=record['pose'][15+chosen]
        reliable_pose=(original[2]>=.65 and all(.015<v<.985 for v in original[:2]))
        pose_fallback=(selected_hand is None and self.locked_side is not None and
                       self.wrist_fallback_s>0 and last_mesh is not None and stamp-last_mesh<=self.wrist_fallback_s and
                       reliable_pose)
        motion_hold=(selected_hand is None and not pose_fallback and self.wrist_fallback_s>0 and
                     last_mesh is not None and stamp-last_mesh<=self.motion_hold_s and reliable_pose)
        if motion_hold:
            if self.detector is not None:
                self.detector.chin_y=chin
                self.detector.update(dict(body_record,hands=[]),stamp,received,source_s,frame,bpm)
            return dict(prepare=False,stop=False,observation=None,visible=False,
                        association='locked_hand_waiting_mesh',**status)
        if selected_hand is None and not pose_fallback:
            self.detector=None
            return dict(prepare=False,stop=False,observation=None,visible=False,
                        association='locked_hand_unavailable',**status)
        body_record=dict(body_record,hands=[selected_hand] if selected_hand is not None else [])
        if self.detector is None:
            self.detector = VisualDetector(chin, aspect=1, dynamic_reference=True,meter=self.meter,
                                           settled_endpoints=self.settled_endpoints)
        self.detector.chin_y = chin
        self.detector.side = self.selected_side
        result = self.detector.update(body_record, stamp, received, source_s, frame, bpm)
        # L arming is evaluated on BOTH hands independently of beat-hand selection.
        result['prepare']=prepare_side is not None and self.control_state in ('idle','paused')
        result['prepare_hand']=prepare_side if result['prepare'] else None
        # Reliable pose can bridge a short finger-mesh gap for movement only.
        # L/chin-crossing/Stop still require actual finger observations.
        if pose_fallback and result['visible']:
            result.update(association='pose_wrist_only',prepare=False,stop=False)
            status['tracking_ready']=True
            if result['observation']:
                result['observation']=replace(result['observation'],confidence=min(.75,self.reference.quality))
        elif result['association'] != 'accepted':
            self.detector = None
            result.update(prepare=False, stop=False, observation=None, visible=False)
        else:
            status['tracking_ready'] = True
            if result['observation']:
                result['observation'] = replace(result['observation'], confidence=min(
                    result['observation'].confidence, self.reference.quality))
        result.update(status)
        return result


def tracking_timeout(now, state, armed_at, latest_reliable, timeout_s):
    """A safety timeout is NOT a recognized Stop gesture."""
    if state not in ('armed', 'running') or armed_at is None:
        return False
    anchor = max(armed_at, latest_reliable if latest_reliable is not None else armed_at)
    return now-anchor >= timeout_s
