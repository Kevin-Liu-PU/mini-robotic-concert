"""Two observed wrist tracks and exactly one tempo source; no camera dependency.

A/B name current track segments, never anatomical or biometric identities.
Missing/ambiguous observations end segments. New segments can acquire anywhere.
"""
from collections import deque
from dataclasses import dataclass, field
from itertools import product
import math

from .continuity import displacement_limit
from .core import Conductor
from .tracking import AssociationConfig, hands_from_result
from .trajectory import Trajectory
from .overlap import choose_overlap, strongly_matches_alternative


@dataclass
class Track:
    slot: str
    generation: int
    hand: object
    started_at: float
    t: float
    sample_dts: deque = field(default_factory=lambda: deque(maxlen=7))
    label_changed: bool = False
    sample_distances: deque = field(default_factory=lambda: deque(maxlen=7))
    observations: deque = field(default_factory=lambda: deque(maxlen=3))

    @property
    def token(self):
        return self.slot, self.generation


class MultiHandTracker:
    """Maximum-cardinality, minimum-distance matching for at most two wrists.

    A mature sole MAIN may reject an irrelevant nearby candidate only with a
    strict observed-continuity certificate. Established two-track crossings and
    similarly plausible identities still pause both tracks. Labels are auxiliary.
    """
    def __init__(self, max_jump=.30, max_gap_s=.25, association=None):
        self.max_jump = max_jump
        self.max_gap_s = max_gap_s
        self.config = association or AssociationConfig()
        self.serial = 0
        self.last_input = None
        # A pause/reset can end identities, but cannot erase recent evidence of
        # two real track segments for the narrow overlap exception. This stores
        # only an observation time, never a missing coordinate or beat history.
        self.recent_two_track_time = None
        self.reset()

    def reset(self):
        self.tracks = {}
        self.last_reason = "awaiting_hands"
        self.last_details = {}
        self.last_decision = {}
        self.competitor = None
        self.overlap_alternative = None

    def _separated(self, best, alternative):
        return (alternative-best >= self.config.continuity_separation and
                alternative >= self.config.continuity_ratio*best)

    def _continued(self, old, observed, t):
        cadence = deque(old.sample_dts,maxlen=7)
        cadence.append(t-old.t)
        distances = deque(old.sample_distances,maxlen=7)
        distances.append(self.distance(old.hand,observed))
        observations = deque(old.observations,maxlen=3)
        observations.append((old.t,old.hand))
        return Track(old.slot,old.generation,observed,old.started_at,t,cadence,
                     observed.label != old.hand.label,distances,observations)

    def _supported_main(self, previous, candidates, primary_token):
        """A mature sole MAIN needs current support, never retroactive speed vetoes."""
        if len(previous) != 1:
            self.last_decision['continuity_check'] = 'requires_sole_prior_track'
            return None
        old = next(iter(previous.values()))
        if old.token != primary_token or len(old.sample_distances) < 3:
            self.last_decision['continuity_check'] = 'requires_established_main'
            return None
        distances = [self.distance(old.hand,h) for h in candidates]
        recent_max = max(list(old.sample_distances)[-3:])
        limit = min(self.config.continuity_radius,
                    displacement_limit(self.max_jump,self.last_input-old.t,old.sample_dts))
        self.last_decision.update(continuity_recent_max_step=recent_max,continuity_limit=limit)
        winner = self._main_hypothesis(old,candidates)
        if winner is None:
            self.last_decision['continuity_check'] = (
                'remembered_competitor_conflict' if self.competitor else 'candidates_not_distinguishable')
            return None
        self.last_decision['continuity_winner_index'] = winner
        # Already accepted historical steps remain diagnostic data. They cannot
        # invalidate a uniquely supported small CURRENT observation.
        if distances[winner] > limit:
            self.last_decision['continuity_check'] = 'motion_outside_local_certificate'
            return None
        self.last_decision['continuity_check'] = 'supported_observed_main'
        return old,winner

    def _main_hypothesis(self, old, candidates):
        """Compare ownership hypotheses once, at the complete-assignment level.

        Two candidates plus memory: [MAIN->0, memory->1] vs the swap.
        One candidate plus memory: MAIN ownership vs memory ownership.
        Memory is an age-bounded observed alternative, never an invented track.
        Reject an infeasible winner; do not force a worse match by discarding it.
        """
        main = [self.distance(old.hand,h) for h in candidates]
        limit = displacement_limit(self.max_jump,self.last_input-old.t,old.sample_dts)
        if len(candidates)==2 and self.distance(*candidates) < self.config.continuity_separation:
            self.last_decision['ownership_check'] = 'candidates_not_distinguishable'
            return None
        if self.competitor:
            other = [self.distance(self.competitor[0],h) for h in candidates]
            if len(candidates)==2:
                costs = [main[0]+other[1],main[1]+other[0]]
                feasible = [main[i]<=limit and other[1-i]<=self.max_jump for i in (0,1)]
                mode = 'main_and_memory_joint_assignment'
            else:
                costs = [main[0],other[0]]
                feasible = [main[0]<=limit,other[0]<=self.max_jump]
                mode = 'single_candidate_ownership'
        else:
            costs = main
            feasible = [d<=limit for d in main]
            mode = 'main_candidate_assignment'
        winner = min(range(len(costs)),key=costs.__getitem__)
        alternative = costs[1-winner] if len(costs)==2 else None
        self.last_decision.update(ownership_mode=mode,ownership_costs=costs,
            ownership_feasible=feasible,ownership_winner=winner,
            ownership_cost_margin=alternative-costs[winner] if alternative is not None else None)
        if not feasible[winner]:
            self.last_decision['ownership_check'] = 'winning_hypothesis_outside_displacement_guard'
            return None
        if alternative is not None and not self._separated(costs[winner],alternative):
            self.last_decision['ownership_check'] = 'hypotheses_not_distinguishable'
            return None
        if mode=='single_candidate_ownership' and winner!=0:
            self.last_decision['ownership_check'] = 'candidate_belongs_to_remembered_alternative'
            return None
        self.last_decision['ownership_check'] = 'supported_observed_main'
        return winner

    def _competitor_allows(self, old, candidates, winner):
        return self.competitor is None or self._main_hypothesis(old,candidates)==winner

    def _overlap_allows(self, old, observed):
        return self.overlap_alternative is None or not strongly_matches_alternative(
            old.hand,self.overlap_alternative[0],observed)

    @staticmethod
    def distance(a, b):
        return math.hypot(a.x-b.x, a.y-b.y)

    def _pause(self, reason):
        self.tracks = {}
        self.competitor = None
        self.overlap_alternative = None
        self.last_reason = reason
        self.last_decision['decision'] = reason
        self.last_details.update(visible_tracks=0, association_state="paused")
        return self.tracks

    def update(self, hands, t, primary_token=None, beat_boundaries=None):
        if not math.isfinite(t) or (self.last_input is not None and t <= self.last_input):
            return False
        dt = t-self.last_input if self.last_input is not None else None
        gap = dt is not None and dt > self.max_gap_s
        self.last_input = t
        candidates = [h for h in hands if all(math.isfinite(v) and 0 <= v <= 1 for v in (h.x,h.y))]
        previous = {} if gap else self.tracks
        if len(self.tracks)==2:
            self.recent_two_track_time = max(track.t for track in self.tracks.values())
        if gap or (self.competitor and t-self.competitor[1] > self.config.distractor_memory_s):
            self.competitor = None
        if gap or (self.overlap_alternative and t-self.overlap_alternative[1] > self.config.distractor_memory_s):
            self.overlap_alternative = None
        slots = sorted(previous)
        matrix = [[self.distance(previous[s].hand,h) for h in candidates] for s in slots]
        limits = [displacement_limit(self.max_jump,t-previous[s].t,previous[s].sample_dts) for s in slots]
        pair_distance = self.distance(*candidates) if len(candidates)==2 else None
        self.last_decision = dict(input_dt_s=dt, prior_slots=slots,
            prior_generations=[previous[s].generation for s in slots],
            candidate_count=len(hands),valid_candidate_indices=[i for i,h in enumerate(hands) if any(h is c for c in candidates)],
            candidate_tip_valid=[h.index_tip is not None for h in hands],
            wrist_distances=matrix,wrist_limits=limits,candidate_pair_distance=pair_distance,
            best_assignment_cost=None,second_assignment_cost=None,assignment_cost_margin=None,
            best_assignment=None,assigned_candidates={},continuity_check='not_needed',suppressed_candidate_indices=[],
            overlap_check='not_applicable',
            recent_two_track_age_s=t-self.recent_two_track_time if self.recent_two_track_time is not None else None,
            competitor_age_s=t-self.competitor[1] if self.competitor else None,
            competitor_distances=[self.distance(self.competitor[0],h) for h in candidates] if self.competitor else [])
        self.last_details = dict(detected_hands=len(hands), valid_wrists=len(candidates),
                                 invalid_wrists=len(hands)-len(candidates),
                                 left_label_hands=sum(h.label=="Left" for h in hands),
                                 right_label_hands=sum(h.label=="Right" for h in hands),
                                 unlabeled_hands=sum(h.label not in ("Left","Right") for h in hands))
        if not hands:
            return self._pause("no_hands")
        if len(candidates) != len(hands):
            return self._pause("invalid_wrist_observation")
        if len(candidates) > 2:
            return self._pause("too_many_detections")
        sole = next(iter(previous.values())) if len(previous)==1 else None
        mature_main = sole is not None and sole.token==primary_token and len(sole.observations)==3
        # Never reinterpret an established or remembered secondary as duplicate.
        # The existing competitor memory remains authoritative and untouched.
        recent_two = (self.recent_two_track_time is not None and
                      t-self.recent_two_track_time <= self.config.distractor_memory_s)
        if mature_main and self.competitor is None and not recent_two:
            if len(candidates)==2:
                winner,details = choose_overlap(sole,candidates,t,self.config,beat_boundaries)
                self.last_decision.update(details)
                if winner is not None and matrix[0][winner] <= limits[0]:
                    if not self._overlap_allows(sole,candidates[winner]):
                        return self._pause('ambiguous_overlap_replacement')
                    self.tracks = {sole.slot:self._continued(sole,candidates[winner],t)}
                    self.overlap_alternative = candidates[1-winner],t
                    self.last_reason = 'main_continues_overlapping_hypotheses'
                    self.last_decision.update(decision=self.last_reason,selected_candidate_index=winner,
                        assigned_candidates={sole.slot:winner},suppressed_candidate_indices=[1-winner])
                    self.last_details.update(visible_tracks=1,association_state='tracking',continued_tracks=1,
                        new_segments=0,label_changes=int(self.tracks[sole.slot].label_changed),unresolved_candidates=1)
                    return self.tracks
        # Enumerate partial injective assignments; prefer continuity only when
        # it is uniquely supported. No prediction or synthetic wrist is used.
        options = []
        for mapping in product(range(-1,len(candidates)), repeat=len(slots)):
            indices = [i for i in mapping if i >= 0]
            if len(set(indices)) != len(indices):
                continue
            cost = 0.0
            for row,(slot,index) in enumerate(zip(slots,mapping)):
                if index < 0:
                    continue
                distance = matrix[row][index]
                if distance > limits[row]:
                    break
                cost += distance
            else:
                options.append((len(indices),cost,mapping))
        options.sort(key=lambda item:(-item[0],item[1]))
        best = options[0]
        second = next((o for o in options[1:] if o[0]==best[0]),None)
        self.last_decision.update(best_matched_tracks=best[0],best_assignment_cost=best[1],
            second_assignment_cost=second[1] if second else None,
            assignment_cost_margin=second[1]-best[1] if second else None,best_assignment=list(best[2]))
        if pair_distance is not None and pair_distance <= self.config.ambiguity_radius:
            supported = self._supported_main(previous,candidates,primary_token)
            if supported is None:
                return self._pause('ambiguous_close_wrists')
            old,winner = supported
            if not self._overlap_allows(old,candidates[winner]):
                return self._pause('ambiguous_overlap_replacement')
            self.tracks = {old.slot:self._continued(old,candidates[winner],t)}
            self.competitor = candidates[1-winner],t
            self.last_reason = 'main_continues_secondary_unresolved'
            self.last_decision.update(decision=self.last_reason,selected_candidate_index=winner,
                                      suppressed_candidate_indices=[1-winner],assigned_candidates={old.slot:winner})
            self.last_details.update(visible_tracks=1,association_state='tracking',continued_tracks=1,
                                     new_segments=0,label_changes=int(self.tracks[old.slot].label_changed),
                                     unresolved_candidates=1)
            return self.tracks
        sole = next(iter(previous.values())) if len(previous)==1 else None
        joint_memory = (sole is not None and sole.token==primary_token and
                        self.competitor is not None and len(candidates)==2)
        if joint_memory:
            # The same ownership evidence applies on either side of the close
            # radius. A per-MAIN distance margin must not veto a clear complete
            # assignment just because the alternative moved slightly farther out.
            winner = self._main_hypothesis(sole,candidates)
            if winner is None:
                return self._pause('ambiguous_recent_competitor')
            best = (1,matrix[0][winner],(winner,))
            self.last_decision['association_scoring'] = 'joint_memory_ownership'
        if (not joint_memory and len(options)>1 and best[0]>0 and options[1][0]==best[0] and
                options[1][1]-best[1] <= self.config.distance_margin):
            return self._pause("ambiguous_assignment")
        current = {}
        used = set()
        for slot,index in zip(slots,best[2]):
            if index < 0:
                continue
            old = previous[slot]
            observed = candidates[index]
            if old.token==primary_token and not joint_memory and not self._competitor_allows(old,candidates,index):
                return self._pause('ambiguous_recent_competitor')
            if old.token==primary_token and not self._overlap_allows(old,observed):
                return self._pause('ambiguous_overlap_replacement')
            current[slot] = self._continued(old,observed,t)
            used.add(index)
        # Fresh slots sort by displayed x only at acquisition. This is not a
        # promise that A remains screen-left after motion or occlusion.
        remaining = sorted((h for i,h in enumerate(candidates) if i not in used),key=lambda h:(h.x,h.y))
        for slot,observed in zip((s for s in ("A","B") if s not in current),remaining):
            self.serial += 1
            current[slot] = Track(slot,self.serial,observed,t,t)
        self.tracks = current
        if len(current)==2:
            self.recent_two_track_time = t
        if len(current)!=1 or next(iter(current.values())).token!=primary_token:
            self.overlap_alternative = None
        if joint_memory and len(current)==2:
            # The alternative is now represented by a real visible track. Do
            # not retain a second stale copy that could veto its future owner.
            self.competitor = None
        elif len(current)==1 and next(iter(current.values())).token==primary_token:
            # If an established secondary track disappears, retain its last
            # actual observation for the same bounded ownership comparison.
            # Disappearance never refreshes the age or draws a missing point.
            missing_secondary = [track for slot,track in previous.items()
                if track.token!=primary_token and slot not in current and
                t-track.t<=self.config.distractor_memory_s]
            if missing_secondary:
                other = missing_secondary[0]
                self.competitor = other.hand,other.t
        self.last_reason = ("reacquired_after_frame_gap" if gap else
                            "new_segments" if remaining else
                            "tracking_label_changed" if any(x.label_changed for x in current.values()) else "tracking")
        self.last_decision['decision'] = self.last_reason
        self.last_decision['assigned_candidates'] = {
            slot:next(i for i,h in enumerate(candidates) if h is track.hand) for slot,track in current.items()}
        self.last_details.update(visible_tracks=len(current),association_state="tracking",
                                 continued_tracks=best[0],new_segments=len(remaining),
                                 label_changes=sum(x.label_changed for x in current.values()))
        return current


class ConductorSession:
    """Shared app/test pipeline: independent trails, one reset-safe beat input."""
    def __init__(self, detector_config=None, association=None, trajectory_seconds=3, preference="any"):
        self.engine = Conductor(detector_config)
        self.config = association or AssociationConfig()
        self.tracker = MultiHandTracker(self.engine.config.max_position_jump,
                                        self.engine.config.max_frame_gap_s,self.config)
        self.trajectories = {s:Trajectory(max(8,trajectory_seconds),self.engine.config.smoothing_tau_s,
                                         self.engine.config.max_frame_gap_s,self.engine.config.max_position_jump)
                             for s in ("A","B")}
        self.preference = preference
        self.last_input = None
        self.last_signature = None
        self.last_result_status = 'not_consumed'
        self._reset_route()

    def _reset_route(self):
        self.primary_token = None
        self.pending_token = None
        self.pending_since = None
        self.initial_choice = True
        self.route_reason = "awaiting_single_hand"
        self.trail_tokens = {}
        self.anchor_missing = False

    @property
    def primary(self):
        return self.primary_token[0] if self.primary_token else None

    @property
    def hand(self):
        track = self.tracker.tracks.get(self.primary)
        return track.hand if track and track.token==self.primary_token else None

    @property
    def beat_anchor(self):
        return self.hand.index_tip if self.hand else None

    def reset(self, reason="manual_reset"):
        self.engine.reset(reason)
        self.tracker.reset()
        for trajectory in self.trajectories.values():
            trajectory.clear()
        self._reset_route()
        self.last_signature = None

    def select_primary(self, slot):
        track = self.tracker.tracks.get(slot)
        if not track or (self.last_input is not None and self.last_input-track.t > self.engine.config.max_frame_gap_s):
            return False
        self._set_primary(track,"manual_primary_selection")
        return True

    def set_beat_line(self, height, t):
        """Move the target without moving tracks or reusing pre-adjustment data."""
        if not math.isfinite(t) or (self.last_input is not None and t < self.last_input):
            raise ValueError("Line adjustment requires a current monotonic timestamp")
        if not self.engine.set_beat_line(height):
            return False
        # Reject a camera/inference sample captured before the key press.
        self.last_input = self.engine.last_input = t
        return True

    def _set_primary(self, track, reason):
        if track.token != self.primary_token:
            self.engine.reset(reason)
            self.primary_token = track.token
            self.engine._note("primary_selected",reason,primary_track=track.slot,
                              primary_generation=track.generation)
        self.initial_choice = False
        self.pending_token = self.pending_since = None
        self.route_reason = reason

    def _route(self, t):
        tracks = self.tracker.tracks
        current = tracks.get(self.primary)
        if self.primary_token and (current is None or current.token != self.primary_token):
            self.engine.reset("primary_track_segment_ended")
            self.primary_token = self.pending_token = self.pending_since = None
        if self.primary_token:
            self.route_reason = "primary_continues"
            return
        candidate = next(iter(tracks.values())) if len(tracks)==1 else None
        # Optional legacy --hand only guides the very first choice. It never
        # filters a track or prevents either hand from returning as a new one.
        if candidate is None and self.initial_choice and self.preference in ("Left","Right"):
            matching = [track for track in tracks.values() if track.hand.label==self.preference]
            candidate = matching[0] if len(matching)==1 else None
        if candidate is None:
            self.pending_token = self.pending_since = None
            self.route_reason = "choose_primary_1_or_2" if len(tracks)==2 else self.tracker.last_reason
            return
        if candidate.token != self.pending_token:
            self.pending_token,self.pending_since = candidate.token,t
        if t-self.pending_since >= self.config.reacquire_s:
            self._set_primary(candidate,"automatic_single_track" if len(tracks)==1 else "initial_label_preference")
        else:
            self.route_reason = "confirming_primary"

    def details(self):
        details = dict(self.tracker.last_details,primary_track=self.primary,primary_reason=self.route_reason)
        for slot in ("A","B"):
            track = self.tracker.tracks.get(slot)
            details.update({f"track_{slot.lower()}_state":"visible" if track else "absent",
                            f"track_{slot.lower()}_generation":track.generation if track else None,
                            f"track_{slot.lower()}_index_tip_valid":track.hand.index_tip is not None if track else False,
                            f"track_{slot.lower()}_model_label":track.hand.label if track else None})
        return details

    def update(self, hands, t, missing_reason=None):
        if not math.isfinite(t) or (self.last_input is not None and t <= self.last_input):
            self.engine._note("sample_ignored","nonmonotonic_or_invalid_time")
            return None
        self.last_input = t
        self.tracker.update(hands,t,self.primary_token,(
            self.engine.config.beat_y-self.engine.config.line_tolerance,
            self.engine.config.beat_y-self.engine.config.rearm_distance))
        if missing_reason:
            self.tracker.last_reason = missing_reason
        for slot,trajectory in self.trajectories.items():
            track = self.tracker.tracks.get(slot)
            anchor = track.hand.index_tip if track else None
            if track and anchor is not None:
                if self.trail_tokens.get(slot) != track.token:
                    trajectory.break_segment()
                self.trail_tokens[slot] = track.token
                trajectory.update(t,*anchor)
            else:
                trajectory.update(t,None,None)
        self._route(t)
        hand = self.hand
        anchor = self.beat_anchor
        if hand and anchor is None and not self.anchor_missing:
            # Valid wrist association does not imply a valid index-tip beat point.
            # End tempo evidence immediately, without changing A/B identity.
            self.engine.reset("invalid_main_index_tip")
        self.anchor_missing = hand is not None and anchor is None
        before = self.engine.diagnostic_sequence
        beat = self.engine.update(t,anchor[0] if anchor else None,anchor[1] if anchor else None,
                                  missing_reason="invalid_main_index_tip" if self.anchor_missing else self.route_reason,
                                  wrist=(hand.x,hand.y) if hand and anchor else None)
        if self.anchor_missing:
            self.engine.status="MAIN INDEX TIP UNAVAILABLE - KEEP THE FINGER VISIBLE"
        for event in self.engine.diagnostic_events:
            if event["seq"] > before:
                event.update(primary_track=self.primary,
                             primary_generation=self.primary_token[1] if self.primary_token else None)
        details = self.details()
        signature = (self.tracker.last_reason,tuple(sorted(details.items())))
        if signature != self.last_signature:
            self.engine._note("selection_status",self.tracker.last_reason,**details)
            self.last_signature = signature
        return beat

    def consume_result(self, result, timestamp, now):
        """Discard stale inference before association or trail state can move."""
        if (not math.isfinite(timestamp) or not math.isfinite(now) or timestamp > now or
                (self.last_input is not None and timestamp <= self.last_input)):
            self.last_result_status = 'nonmonotonic_or_invalid_result'
            self.engine._note("sample_ignored","nonmonotonic_or_invalid_result")
            return False,None
        if now-timestamp > self.engine.config.max_frame_gap_s:
            self.last_result_status = 'inference_result_too_old'
            self.update([],now,"inference_result_too_old")
            return False,None
        self.last_result_status = 'accepted'
        return True,self.update(hands_from_result(result),timestamp)

    def display_status(self):
        if self.primary:
            if self.anchor_missing:
                return f"MAIN {self.primary} | index tip unavailable"
            if self.tracker.last_reason=='main_continues_secondary_unresolved':
                return f"MAIN {self.primary} | extra candidate unresolved"
            if self.tracker.last_reason=='main_continues_overlapping_hypotheses':
                return f"MAIN {self.primary} | overlapping hypotheses; observed tip"
            return f"MAIN {self.primary} | 1=A  2=B | {len(self.tracker.tracks)} visible"
        if self.route_reason=="confirming_primary":
            return "Confirming single hand; auto MAIN soon"
        if self.route_reason=="choose_primary_1_or_2":
            return "Two tracks: press 1=A or 2=B for MAIN"
        if self.tracker.last_reason.startswith("ambiguous"):
            return "Ambiguous: separate hands to resume"
        return "No MAIN; any single hand resumes"
