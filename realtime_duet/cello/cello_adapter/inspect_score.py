"""Read teacher MXL locally if materialized; never imports upstream robot code."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile


def inspect(path):
    path = Path(path)
    if path.suffix.lower() == ".mxl":
        with zipfile.ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            file = next(n.attrib["full-path"] for n in container.iter() if n.tag.split("}")[-1] == "rootfile")
            root = ET.fromstring(archive.read(file))
    else:
        root = ET.parse(path).getroot()
    names = {p.attrib["id"]: p.findtext("part-name", "") for p in root.findall("part-list/score-part")}
    parts = [p for p in root.findall("part") if "cello" in names.get(p.attrib["id"], "").lower()]
    if len(parts) != 1:
        raise ValueError("expected exactly one named cello part")
    events, onset, divisions = [], 0.0, None
    for measure in parts[0].findall("measure"):
        div = measure.findtext("attributes/divisions")
        if div:
            divisions = float(div)
        notes = measure.findall("note")
        if not divisions or len(notes) != 1 or measure.find("backup") is not None:
            raise ValueError("expected one whole note per complete measure")
        note = notes[0]
        duration = float(note.findtext("duration")) / divisions
        pitch = note.find("pitch")
        if duration != 4 or pitch is None or note.find("chord") is not None:
            raise ValueError("expected four-beat pitched cello note")
        if pitch.findtext("alter", "0") != "0":
            raise ValueError("unexpected pitch alteration; inspect score")
        events.append(dict(measure=measure.attrib["number"], onset_beats=onset,
                           duration_beats=duration, pitch=pitch.findtext("step") + pitch.findtext("octave")))
        onset += duration
    if len(events) != 12 or onset != 48:
        raise ValueError("expected 12 complete 4/4 measures, 48 beats")
    return events


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("mxl")
    args = p.parse_args()
    print(json.dumps(inspect(args.mxl), indent=2))
