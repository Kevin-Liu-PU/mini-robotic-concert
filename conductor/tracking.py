"""Detection adapters and the legacy single-track regression reference.

The running app uses multitrack.ConductorSession, not HandSelector.
"""
from dataclasses import dataclass
from collections import Counter,deque
import math
from .continuity import displacement_limit

INDEX_TIP_LANDMARK = 8


def index_tip_position(points):
    """Only the observed index fingertip; no averaging or alternate-point fallback."""
    if points is None or len(points) <= INDEX_TIP_LANDMARK:
        return None
    try:
        point = points[INDEX_TIP_LANDMARK]
        coordinates = (point.x, point.y)
        valid = all(math.isfinite(v) and 0 <= v <= 1 for v in coordinates)
    except (AttributeError, TypeError, ValueError):
        return None
    if not valid:
        return None
    return coordinates


@dataclass
class Hand:
    """x/y remain the wrist used for identity association, separate from the tip."""
    x: float
    y: float
    label: str | None
    label_score: float
    landmarks: object = None

    @property
    def index_tip(self):
        return index_tip_position(self.landmarks)


@dataclass(frozen=True)
class AssociationConfig:
    ambiguity_radius: float = .12
    distance_margin: float = .08
    reacquire_s: float = .25
    distractor_memory_s: float = .5
    continuity_radius: float = .03
    continuity_separation: float = .02
    continuity_ratio: float = 3.0
    overlap_palm_fraction: float = .25
    overlap_shape_fraction: float = .6
    overlap_motion_ratio: float = 2.0
    overlap_motion_margin: float = .15

    def __post_init__(self):
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or
               not math.isfinite(v) or v<=0 for v in vars(self).values()):
            raise ValueError('Association settings must be finite positive numbers')
        if max(self.ambiguity_radius,self.distance_margin,self.continuity_radius,
               self.continuity_separation)>1:
            raise ValueError('Association distances must not exceed normalized image size')
        if min(self.continuity_ratio,self.overlap_motion_ratio) <= 1:
            raise ValueError('Association evidence ratios must be greater than one')


def hands_from_result(result):
    """Keep unlabeled detections visible to diagnostics; never invent a label."""
    hands=[]
    for i,points in enumerate(result.hand_landmarks):
        if not points:
            continue
        categories=result.handedness[i] if i<len(result.handedness) else []
        category=categories[0] if categories else None
        label=category.category_name if category else None
        if label not in ("Left","Right"):
            label=None
        hands.append(Hand(points[0].x,points[0].y,label,
                          category.score if category else 0.0,points))
    return hands


def select_fresh_result(selector,result,timestamp,now,max_age,after_timestamp=None):
    """Reject unusable inference BEFORE it can acquire or move a track."""
    if (not math.isfinite(timestamp) or not math.isfinite(now) or timestamp>now or
            (after_timestamp is not None and timestamp<=after_timestamp)):
        reason="nonmonotonic_observation"
    elif now-timestamp>max_age:
        reason="inference_result_too_old"
    else:
        return selector.choose(hands_from_result(result),timestamp)
    # Diagnostic fields can change, but label/position/cadence state must not.
    selector.last_reason=reason
    selector.last_details={"label_hint":selector.label,"result_discarded":True}
    return None


class HandSelector:
    """Legacy selector retained for prior regression comparisons, not app routing.

    This is not biometric identity. Close competitors stop the track until R,
    and a long absence requires a stable return near the last observed wrist.
    Only real selected observations are returned, never interpolated positions.
    """
    def __init__(self,preference="any",max_jump=.30,brief_loss_s=.12,association=None):
        self.preference=preference
        self.max_jump=max_jump
        self.brief_loss_s=brief_loss_s
        self.config=association or AssociationConfig()
        self.reset()

    def reset(self):
        self.label=None                 # Recent model-label hint, not an ID.
        self.labels=deque(maxlen=7)
        self.previous=None
        self.previous_t=None
        self.last_input=None
        self.sample_dts=deque(maxlen=7)
        self.others=[]                  # Recently observed non-target wrists.
        self.needs_reset=False
        self.pending=None
        self.pending_since=None
        self.pending_t=None
        self.last_reason="awaiting_hand"
        self.last_details={}

    @staticmethod
    def distance(a,b):
        return math.hypot(a.x-b.x,a.y-b.y)

    def _reject(self,reason,state="lost",sticky=False,keep_pending=False):
        self.last_reason=reason
        self.needs_reset=self.needs_reset or sticky
        self.last_details.update(association_state=state,requires_reset=self.needs_reset)
        if not keep_pending:
            self.pending=self.pending_since=self.pending_t=None
        return None

    def _accept(self,chosen,candidates,timestamp):
        changed=self.previous is not None and chosen.label!=self.previous.label
        dt=timestamp-self.previous_t if timestamp is not None and self.previous_t is not None else None
        if dt is not None and 0<dt<=self.brief_loss_s:
            self.sample_dts.append(dt)
        elif dt is not None:
            self.sample_dts.clear()
        self.previous=chosen
        self.previous_t=timestamp
        if chosen.label in ("Left","Right"):
            self.labels.append(chosen.label)
            counts=Counter(self.labels)
            self.label=max(counts,key=lambda label:(counts[label],label==self.label))
        other=[h for h in candidates if h is not chosen]
        if other:
            self.others=[(h,timestamp) for h in other]
        elif timestamp is not None:
            self.others=[(h,t) for h,t in self.others if t is None or timestamp-t<=self.config.distractor_memory_s]
        self.pending=self.pending_since=self.pending_t=None
        self.last_reason="selected_label_changed" if changed else "selected"
        self.last_details.update(association_state="tracking",requires_reset=False,
                                 label_hint=self.label,selected_model_label=chosen.label,
                                 model_label_changed=changed)
        return chosen

    def choose(self,hands,timestamp=None):
        if timestamp is not None:
            if not math.isfinite(timestamp) or (self.last_input is not None and timestamp<=self.last_input):
                self.last_reason="nonmonotonic_observation"
                return None
            self.last_input=timestamp
        candidates=[h for h in hands if all(math.isfinite(v) and 0<=v<=1 for v in (h.x,h.y))]
        self.last_details={"detected_hands":len(hands),"valid_wrists":len(candidates),
                           "invalid_wrists":len(hands)-len(candidates),
                           "unlabeled_hands":sum(h.label not in ("Left","Right") for h in hands),
                           "left_label_hands":sum(h.label=="Left" for h in hands),
                           "right_label_hands":sum(h.label=="Right" for h in hands),
                           "label_hint":self.label,"requires_reset":self.needs_reset}
        if self.needs_reset:
            return self._reject("ambiguous_identity_reset_required","ambiguous",sticky=True)
        if not candidates:
            reason=("no_hands" if not hands else "landmark_nonfinite" if
                    any(not math.isfinite(v) for h in hands for v in (h.x,h.y)) else "landmark_out_of_frame")
            return self._reject(reason)
        if len(candidates)!=len(hands):
            # An invalid second wrist might be the target. Never silently adopt
            # the other valid hand just because its coordinates are usable.
            return self._reject("invalid_competing_detection","ambiguous",
                                sticky=self.previous is not None)
        if self.previous is None:
            if len(candidates)==1:
                chosen=candidates[0]
            else:
                preferred=[h for h in candidates if h.label==self.preference]
                if self.preference=="any" or len(preferred)!=1:
                    return self._reject("multiple_hands_waiting","waiting")
                chosen=preferred[0]
                if any(self.distance(chosen,h)<=self.config.ambiguity_radius for h in candidates if h is not chosen):
                    return self._reject("multiple_hands_waiting","waiting")
            return self._accept(chosen,candidates,timestamp)

        dt=timestamp-self.previous_t if timestamp is not None and self.previous_t is not None else None
        # A long loss never expands the allowed region without bound.
        allowed=(displacement_limit(self.max_jump,dt,self.sample_dts)
                 if dt is None or dt<=self.brief_loss_s else self.max_jump)
        ordered=sorted(candidates,key=lambda h:self.distance(h,self.previous))
        chosen=ordered[0]
        distance=self.distance(chosen,self.previous)
        nearby=[h for h in ordered if self.distance(h,self.previous)<=allowed]
        self.last_details.update(near_anchor_hands=len(nearby),
                                 nearest_distance=round(distance,4),allowed_distance=round(allowed,4))
        if not nearby:
            return self._reject("association_position_jump")
        if len(ordered)>1:
            second=ordered[1]
            if (self.distance(chosen,second)<=self.config.ambiguity_radius or
                    (len(nearby)>1 and self.distance(second,self.previous)-distance<=self.config.distance_margin)):
                return self._reject("ambiguous_candidates","ambiguous",sticky=True)
        for other,observed_t in self.others:
            recent=(timestamp is None or observed_t is None or
                    timestamp-observed_t<=self.config.distractor_memory_s or
                    (dt is not None and dt>self.brief_loss_s))
            other_distance=self.distance(chosen,other)
            if (recent and other_distance<=allowed and
                    other_distance<=distance+self.config.distance_margin):
                return self._reject("possible_other_hand","ambiguous",sticky=True)

        if dt is not None and dt>self.brief_loss_s:
            stable=(self.pending is not None and self.pending_t is not None and
                    timestamp-self.pending_t<=self.brief_loss_s and
                    self.distance(chosen,self.pending)<=self.max_jump)
            if not stable:
                self.pending_since=timestamp
            self.pending=chosen
            self.pending_t=timestamp
            if timestamp-self.pending_since<self.config.reacquire_s:
                return self._reject("reacquiring_near_anchor","reacquiring",keep_pending=True)
        return self._accept(chosen,candidates,timestamp)

    def display_status(self):
        if self.needs_reset:
            return "Ambiguous hands: R with one hand visible"
        if self.last_reason=="multiple_hands_waiting":
            return "Show one hand to choose a track"
        if self.last_reason=="reacquiring_near_anchor":
            return "Reacquiring near previous wrist"
        if self.last_reason=="association_position_jump":
            return "Outside track area; return or press R"
        if self.last_reason=="no_hands":
            return "No hand detected; awaiting return"
        if self.last_reason in ("landmark_nonfinite","landmark_out_of_frame"):
            return "Hand detected; invalid wrist filtered"
        if self.last_reason.startswith("selected"):
            return f"Spatial track; model label {self.previous.label or 'unknown'}"
        return self.last_reason.replace('_',' ')
