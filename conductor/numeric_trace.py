"""Optional local per-processed-frame evidence; no image or landmark coordinates."""
import json
import math

from .tracking import hands_from_result


def result_counts(result):
    hands = hands_from_result(result)
    return dict(model_candidate_count=len(hands),
                model_valid_wrists=sum(all(math.isfinite(v) and 0<=v<=1 for v in (h.x,h.y)) for h in hands),
                model_valid_tips=sum(h.index_tip is not None for h in hands))


def frame_record(session, *, start, timestamp, completed_at, sequence, synthetic,
                 accepted, inference_ms, preparation_ms, association_ms, processing_ms,
                 frame_age_at_start_ms, counts):
    return dict(schema_version=1, event='processed_frame', synthetic=synthetic,
                sequence=sequence, sample_time_s=timestamp-start,
                completed_time_s=completed_at-start,
                frame_age_at_start_ms=frame_age_at_start_ms,
                result_age_ms=(completed_at-timestamp)*1000,
                inference_ms=inference_ms, preparation_ms=preparation_ms,
                association_ms=association_ms, processing_ms=processing_ms,
                accepted=accepted, result_status='synthetic' if synthetic else session.last_result_status,
                association=session.tracker.last_decision if accepted else None,
                routing_after=session.details(), bpm=session.engine.bpm,
                tempo_state=session.engine.tempo_state, **counts)


class NumericTrace:
    """Buffer to avoid opening a file on every frame; flush each 30 records/exit."""
    def __init__(self,path):
        self.stream = path.open('a',encoding='utf-8')
        self.records = 0

    def write(self,record):
        # Strict serialization rejects NaN/Infinity rather than writing invalid JSON.
        self.stream.write(json.dumps(record,allow_nan=False,separators=(',',':'))+'\n')
        self.records += 1
        if self.records % 30 == 0 or record.get('kind') in (
                'AUDIO_CONTROL', 'CLOCK_CONTROL', 'SYSTEM_FAULT', 'LOOP_GAP_CONTEXT'):
            self.stream.flush()

    def close(self):
        self.stream.close()
