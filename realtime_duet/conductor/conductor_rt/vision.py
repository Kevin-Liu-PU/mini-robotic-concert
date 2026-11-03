"""Single-Holistic adapter and causal observation extractor.

The L/raised-pinch gestures are this project's conventions, not universal
conducting gestures. Quality values are heuristic, not calibrated probabilities.
"""
from collections import deque
import math
import statistics
from .clock import BeatObservation


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def hand_features(points, aspect):
    # x and y expressed in common image-height units.
    p = [(x * aspect, y) for x, y in points]
    palm = max(distance(p[5], p[17]), .001)
    def angle(a, b, c):
        u = (p[a][0] - p[b][0], p[a][1] - p[b][1])
        v = (p[c][0] - p[b][0], p[c][1] - p[b][1])
        dot = sum(x*y for x, y in zip(u, v)) / max(1e-9, math.hypot(*u)*math.hypot(*v))
        return math.degrees(math.acos(max(-1., min(1., dot))))
    return dict(pinch=distance(p[4], p[8])/palm,
                index_extension=distance(p[8], p[0])/max(1e-9, distance(p[6], p[0])),
                index_angle=angle(5, 6, 8), thumb_spread=distance(p[4], p[5])/palm,
                other_extension=statistics.median(distance(p[j], p[0])/max(1e-9, distance(p[j-2], p[0])) for j in (12, 16, 20)))


def l_shape_conditions(hand, chin_y, aspect, features=None):
    """Same explicit L definition as baseline; no thresholds relaxed."""
    f=features if features is not None else hand_features(hand,aspect)
    ix,iy=(hand[8][0]-hand[5][0])*aspect,hand[8][1]-hand[5][1]
    tx,ty=(hand[4][0]-hand[5][0])*aspect,hand[4][1]-hand[5][1]
    il,tl=math.hypot(ix,iy),math.hypot(tx,ty)
    valid=il>1e-6 and tl>1e-6
    return dict(wrist_above_chin=bool(hand[0][1]<chin_y),
                index_up=bool(valid and -iy/il>=.75),
                thumb_crosswise=bool(valid and abs(ix*tx+iy*ty)/(il*tl)<=.75),
                index_straight=bool(f['index_angle']>165),
                thumb_spread=bool(f['thumb_spread']>.65),
                thumb_index_apart=bool(f['pinch']>1.5),
                other_fingers_folded=bool(f['other_extension']<.85))

def result_record(result):
    pose = result.pose_landmarks
    from .body_reference import FACE_POINTS
    face = getattr(result, "face_landmarks", None)
    return dict(face={str(i): [float(face.landmark[i].x), float(face.landmark[i].y), float(face.landmark[i].z)]
                      for i in FACE_POINTS} if face and len(face.landmark) > max(FACE_POINTS) else None,
                pose=[[float(p.x), float(p.y), float(p.visibility)] for p in pose.landmark] if pose else None,
                hands=[dict(label=label, points=[[float(p.x), float(p.y)] for p in hand.landmark])
                       for label, hand in (("Left", result.left_hand_landmarks), ("Right", result.right_hand_landmarks)) if hand])


class VisualDetector:
    def __init__(self, chin_y, aspect=16/9, geometry="native", dynamic_reference=False, meter=4, settled_endpoints=True):
        if not 0 < chin_y < 1:
            raise ValueError("explicit chin calibration must be in (0, 1)")
        self.chin_y = chin_y
        self.dynamic_reference = dynamic_reference
        self.meter=meter
        self.settled_endpoints=settled_endpoints
        self.motion_feature=None
        self.inward_sign=None
        self.crossing_anchor_tip = None
        self.aspect = aspect if geometry == "native" else 720/1280
        self.side = None
        self.scale_history = deque()
        self.motion = deque()
        self.scale = None
        self.last_frame_stamp = None
        self.low = None
        self.peak = None
        self.down_armed = False
        self.previous_tip = None
        self.tip_anchor_wrist = None
        self.was_below = False
        self.last_detection = None
        self.l_since = None
        self.prepare_sent = False
        self.last_approach = None
        self.stop_approach_stamp = None
        self.last_hand = None
        self.stop_window = deque()
        self.stroke_motion = deque()

    def stroke_kind(self, incoming, endpoint_y):
        """The configured pattern's downward/interior strokes, never upstroke."""
        down=incoming[1]>=.40*self.scale and incoming[1]>.7*abs(incoming[0])
        sideways=abs(incoming[0])>=.40*self.scale and abs(incoming[0])>=1.5*abs(incoming[1])
        sideways=sideways and endpoint_y>self.chin_y+.04*self.scale
        if self.meter==3:sideways=sideways and self.inward_sign is not None and incoming[0]*self.inward_sign>0
        return 'wrist_rebound' if down else 'lateral_turn' if sideways else None

    def settled_endpoint(self, history, received):
        """Confirm arrival after at least 60 ms of observed near-stillness.

        The timestamp is the beginning of that observed window, not the time
        this conclusion became available. A qualifying .40-shoulder stroke
        must precede it; a stationary hand, translation or a slow straight
        sweep alone cannot qualify. No samples beyond the current frame exist.
        """
        if len(history)<4:return None
        now=history[-1][0]
        first=len(history)-1
        while first>0 and (now-history[first][0]<.06 or len(history)-first<3):first-=1
        tail=history[first:]
        if first==0 or len(tail)<3:return None
        elapsed=now-tail[0][0]
        if elapsed>.15:return None
        extent=math.hypot(max(row[1][0] for row in tail)-min(row[1][0] for row in tail),
                          max(row[1][1] for row in tail)-min(row[1][1] for row in tail))
        if extent>.05*self.scale:return None
        speed=distance(tail[-1][1],tail[0][1])/elapsed
        pivot=tail[0]
        for anchor in reversed(history[:first]):
            incoming=(pivot[1][0]-anchor[1][0],pivot[1][1]-anchor[1][1])
            kind=self.stroke_kind(incoming,pivot[1][1])
            if kind is None:continue
            approach_speed=math.hypot(*incoming)/(pivot[0]-anchor[0])
            if speed<=.25*approach_speed:
                self.motion_feature='settled_endpoint'
                return BeatObservation(pivot[0],received,.80,kind,pivot[3],pivot[2])
        return None

    def stroke_turn(self, body, stamp, received, source_s, frame):
        """Observe a downward or sideways stroke turning at least 60 degrees.

        Uses the same .40-shoulder stroke and .10-shoulder confirmation
        distances as the vertical rebound. A straight sweep or a stationary
        hand is insufficient. Only the automatic body-relative path uses it.
        The extremum timestamp and the later decision remain separate.
        """
        point=(body[0]*self.aspect,body[1])
        self.stroke_motion.append((stamp,point,source_s,frame))
        while self.stroke_motion and stamp-self.stroke_motion[0][0]>1.5:
            self.stroke_motion.popleft()
        history=list(self.stroke_motion)
        self.motion_feature=None
        if self.settled_endpoints:
            arrival=self.settled_endpoint(history,received)
            if arrival is not None:return arrival
        # Prefer the freshest confirmed corner; never report a stale pivot.
        for j in range(len(history)-2,0,-1):
            pivot=history[j]
            age=stamp-pivot[0]
            if age<.04:continue
            if age>.30:break
            outgoing=(point[0]-pivot[1][0],point[1]-pivot[1][1])
            outgoing_length=math.hypot(*outgoing)
            if outgoing_length<.10*self.scale:continue
            for anchor in reversed(history[:j]):
                incoming=(pivot[1][0]-anchor[1][0],pivot[1][1]-anchor[1][1])
                length=math.hypot(*incoming)
                if length<.40*self.scale:continue
                # The upper return is the final/upstroke cue, not another
                # sideways beat. In the configured three-beat pattern the
                # interior stroke travels inward; four beats can use both
                # inward and outward strokes. These are explicit conventions,
                # never inferred bar counts or per-recording time windows.
                kind=self.stroke_kind(incoming,pivot[1][1])
                if kind is None:continue
                cosine=sum(a*b for a,b in zip(incoming,outgoing))/(length*outgoing_length)
                if cosine<=.5:
                    self.motion_feature='direction_turn'
                    return BeatObservation(pivot[0],received,.80,kind,pivot[3],pivot[2])
        return None

    def confirm_stop(self, stamp, body, strong_shape, approach):
        """One shared candidate: sustained high pinch plus a settled wrist.

        220 ms of evidence is an explicit added latency, never backdated away.
        This experimental rule is not a replacement for an operator Stop.
        """
        if self.stop_window and stamp-self.stop_window[-1][0] > .08:
            self.stop_window.clear()
        self.stop_window.append((stamp,body,strong_shape,approach))
        while self.stop_window and stamp-self.stop_window[0][0] > .28:
            self.stop_window.popleft()
        fraction=sum(x[2] for x in self.stop_window)/len(self.stop_window)
        points=[(row[1][0]*self.aspect,row[1][1]) for row in self.stop_window]
        extent=math.hypot(max(x for x,y in points)-min(x for x,y in points),max(y for x,y in points)-min(y for x,y in points))
        # Fresh approach must coincide with observed strong shape inside this
        # bounded confirmation window, rather than expire just before its end.
        # Missing mesh is a negative shape sample, never a held pinch.
        entered=any(row[2] and row[3] for row in self.stop_window)
        self.stop_checks=dict(shape=bool(strong_shape),approach=bool(entered),
                              frames=len(self.stop_window)>=6,
                              duration=stamp-self.stop_window[0][0]>=.22,
                              fraction=fraction>=.75,stillness=extent<=.12*self.scale)
        confirmed=all(self.stop_checks.values())
        return confirmed,self.stop_window[0][0] if confirmed else None

    def update(self, record, stamp, received, source_s, frame, bpm):
        pose = record["pose"]
        result = dict(prepare=False, stop=False, observation=None, visible=False, features=None,
                      body=None, tip=None, scale=self.scale, association="body_missing")
        if self.last_frame_stamp is not None and stamp - self.last_frame_stamp > .15:
            self.low = self.peak = self.previous_tip = None
            self.tip_anchor_wrist = None
            self.down_armed = self.was_below = False
            self.crossing_anchor_tip = None
            self.l_since = None
            self.stroke_motion.clear()
            # A verified body reference may survive a brief scheduling gap,
            # but motion, L and Stop evidence may not bridge unseen movement.
            self.motion.clear()
            self.last_approach = None
            self.stop_approach_stamp = None
            self.stop_window.clear()
        self.last_frame_stamp = stamp
        if not pose or min(pose[i][2] for i in (11, 12)) < .65:
            self.previous_tip = None
            self.was_below = False
            self.crossing_anchor_tip = None
            self.l_since = None
            return result
        if self.side is None:
            self.side = 0 if pose[11][0] > pose[12][0] else 1
        wi = 15 + self.side
        self.inward_sign=1 if pose[11+self.side][0]<(pose[11][0]+pose[12][0])/2 else -1
        p = [(x*self.aspect, y) for x, y, _ in pose]
        scale = distance(p[11], p[12])
        if .03 < scale < .8:
            self.scale_history.append((stamp, scale))
        while self.scale_history and stamp - self.scale_history[0][0] > .5:
            self.scale_history.popleft()
        if self.scale_history:
            self.scale = statistics.median(s for _, s in self.scale_history)
        if not self.scale or pose[wi][2] < .65:
            self.l_since = None
            return result
        body = pose[wi][:2]
        if not all(math.isfinite(v) and (self.dynamic_reference or 0 <= v <= 1) for v in body):
            return result
        result.update(body=body, scale=self.scale, visible=True, association="hand_missing")
        self.motion.append((stamp, body))
        while self.motion and stamp-self.motion[0][0] > .12:
            self.motion.popleft()
        if len(self.motion) > 1:
            old = self.motion[0][1]
            dx, dy = (body[0]-old[0])*self.aspect, body[1]-old[1]
            head_x = pose[0][0]
            if math.hypot(dx, dy) > .12*self.scale:
                outward = abs(body[0]-head_x) > abs(old[0]-head_x)
                direction = "down" if dy > 0 and dy > .25*abs(dx) else "outward" if outward and abs(dx) > .7*abs(dy) else "up_or_inward"
                self.last_approach = (stamp, direction)
                # Stop has its own short-lived approach evidence. An upward
                # diagonal remains an approach even when its horizontal part
                # is outward. A final sideways settling motion must not erase
                # the observed ascent; a new downstroke cancels it.
                up = dy < 0 and -dy > .25*abs(dx)
                inward = not outward and abs(dx) > .7*abs(dy)
                if direction == 'down':
                    self.stop_approach_stamp = None
                    self.stop_window.clear()
                elif up or inward:
                    self.stop_approach_stamp = stamp
        hand = None
        candidates = [h for h in record["hands"] if all(math.isfinite(v) and (self.dynamic_reference or .015 <= v <= .985) for xy in h["points"] for v in xy)]
        if candidates:
            nearest = min(candidates, key=lambda h: distance(h["points"][0], body))
            if distance(nearest["points"][0], body) <= .15:
                hand = nearest["points"]
                result["association"] = "accepted"
        tip = None
        if hand:
            tip = hand[8]
            f = hand_features(hand, self.aspect)
            result.update(features=f, tip=tip)
            raised = hand[0][1] < self.chin_y
            conditions=l_shape_conditions(hand,self.chin_y,self.aspect,f)
            lshape=all(conditions.values())
            result['l_conditions']=conditions
            if lshape:
                if self.l_since is None:
                    self.l_since = stamp
                if stamp-self.l_since >= .15 and not self.prepare_sent:
                    result["prepare"] = True
                    self.prepare_sent = True
            else:
                self.l_since = None
                self.prepare_sent = False
            approach = self.last_approach and stamp-self.last_approach[0] <= .6 and self.last_approach[1] == "up_or_inward"
            result["legacy_instant_stop"] = bool(raised and approach and f["pinch"] < .45 and f["other_extension"] < .95 and f["index_angle"] < 150 and 1 <= f["index_extension"] <= 1.35)
            # Holistic often estimates the demonstrator's pinched index nearly
            # straight. Replace that unreliable angle gate with explicitly
            # confirmed high-hand stillness, not a per-video time window.
            # User-defined shared reference: Stop and L use this SAME tracked
            # chin line. A raised wrist alone never means Stop; pinch, folded
            # fingers, fresh approach and sustained stillness remain required.
            shape_checks=dict(wrist_above_chin=bool(raised),
                              pinch=f['pinch']<.45,other_fingers=f['other_extension']<.95,
                              index_extension=.6<=f['index_extension']<=1.5)
            strong=all(shape_checks.values())
            stop_approach_stamp = getattr(self, 'stop_approach_stamp', None)
            stop_approach = stop_approach_stamp is not None and 0 <= stamp-stop_approach_stamp <= .6
            result["stop"],result["stop_evidence_stamp"]=self.confirm_stop(stamp,body,strong,stop_approach)
            result["stop_confirmation_s"]=stamp-result["stop_evidence_stamp"] if result["stop"] else None
            result.update(L=bool(lshape), raised=raised, stop_approach=bool(stop_approach),
                          stop_approach_age_s=None if stop_approach_stamp is None else stamp-stop_approach_stamp,
                          stop_shape_valid=bool(strong),stop_checks=dict(self.stop_checks,**shape_checks))
        else:
            self.l_since = None
            self.confirm_stop(stamp,body,False,False)
        candidate = None
        feature=None
        turn = (self.stroke_turn(body,stamp,received,source_s,frame)
                   if self.dynamic_reference else None)
        if tip:
            previous = self.previous_tip
            if tip[1] >= self.chin_y + .04*self.scale:
                self.was_below = True
                self.crossing_anchor_tip = max(tip[1], self.crossing_anchor_tip or tip[1])
                self.tip_anchor_wrist = body
            real_upstroke = (not self.dynamic_reference or self.crossing_anchor_tip is not None and
                             self.crossing_anchor_tip-tip[1] >= .12*self.scale)
            gap_limit=min(.30,.45*60/bpm)
            gap=stamp-previous[0] if previous else float('inf')
            # A finger-mesh gap may be bridged only by continuous reliable
            # wrist ascent, not by a moving reference or an unseen body gap.
            bridged=(gap>.12 and self.tip_anchor_wrist is not None and
                     self.tip_anchor_wrist[1]-body[1]>=.12*self.scale)
            if real_upstroke and self.was_below and previous and gap <= gap_limit and (gap<=.12 or bridged) and tip[1] < previous[1][1] and tip[1] <= self.chin_y - .025*self.scale:
                self.was_below = False
                self.crossing_anchor_tip = None
                candidate = BeatObservation(stamp, received, .80, "chin_crossing", frame, source_s)
                feature='chin_crossing_reacquired' if bridged else 'chin_crossing'
            self.previous_tip = (stamp, tip)
        else:
            if self.previous_tip is None or stamp-self.previous_tip[0]>min(.30,.45*60/bpm):
                self.previous_tip = None
                self.was_below = False
                self.crossing_anchor_tip = None
                self.tip_anchor_wrist = None
        x, y = body
        if self.low is None:
            self.low = y
            self.peak = (stamp, y, source_s, frame)
        if not self.down_armed:
            self.low = min(self.low, y)
            if y-self.low >= .4*self.scale:
                self.down_armed = True
                self.peak = (stamp, y, source_s, frame)
        if self.down_armed:
            if y >= self.peak[1]:
                self.peak = (stamp, y, source_s, frame)
            if self.peak[1]-y >= .10*self.scale and stamp-self.peak[0] >= .04:
                if candidate is None:
                    candidate = BeatObservation(self.peak[0], received, .85, "wrist_rebound", self.peak[3], self.peak[2])
                    feature='vertical_rebound'
        # A curved descending stroke can already turn sideways before its
        # lowest endpoint. Do not let that bend consume/debounce the real
        # downbeat; pure horizontal strokes and down-to-side endpoints remain.
        if candidate is None and turn is not None and not (self.down_armed and turn.kind=='lateral_turn'):
            candidate=turn;feature=self.motion_feature
        if candidate:
            from dataclasses import replace
            candidate = replace(candidate, decision_stamp=stamp, decision_frame=frame)
            result.update(candidate_kind=candidate.kind,candidate_stamp=candidate.stamp,
                          candidate_frame=candidate.frame,candidate_feature=feature,
                          candidate_confirmation_ms=(stamp-candidate.stamp)*1000)
            # De-bounce observed event times, not delayed decision times.
            # Keep gathering the NEXT stroke during this interval instead of
            # erasing its beginning on every refractory frame.
            # Multiple bends within one stroke must not accelerate the clock.
            # Keep a 10% margin below a half-period, so a deliberate 2x
            # cadence can still supply distinct observations.
            if self.last_detection is not None and candidate.stamp-self.last_detection < .45*60/bpm:
                result['beat_rejection']='same_stroke_refractory'
                candidate=None
            else:
                self.last_detection = candidate.stamp
            self.low = self.peak = None
            self.down_armed = False
            self.stroke_motion.clear()
        result['beat_stage']='wait_rebound' if self.down_armed else 'wait_downstroke'
        result['downstroke_shoulder_units']=max(0.,(body[1]-self.low)/self.scale) if self.low is not None else 0.
        result['rebound_shoulder_units']=max(0.,(self.peak[1]-body[1])/self.scale) if self.peak is not None else 0.
        result["observation"] = candidate
        return result
