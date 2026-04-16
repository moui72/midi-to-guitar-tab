"""
Stage 1: Parse MIDI -> extract chords + strum hints -> song_data.json
Usage: python extract_chords.py input.mid [bpm_override]
"""

import sys
import json
import logging
import re
from pathlib import Path
from music21 import converter, tempo, meter, harmony, chord as m21chord
import pretty_midi

logger = logging.getLogger(__name__)


DURATION_MAP = {
    4.0: (1, "whole"), 3.0: (2, "dotted half"), 2.0: (2, "half"),
    1.5: (4, "dotted quarter"), 1.0: (4, "quarter"),
    0.75: (8, "dotted eighth"), 0.5: (8, "eighth"),
    0.25: (16, "sixteenth"), 1/3: (8, "eighth triplet"),
}


def quantize_duration(ql):
    """Snap a music21 quarterLength to nearest standard duration."""
    candidates = list(DURATION_MAP.keys())
    closest = min(candidates, key=lambda x: abs(x - ql))
    return DURATION_MAP[closest]


def simplify_chord_name(chord_obj):
    """
    Convert music21 Chord object to standard chord symbol (e.g. Dm, F, C, Am7).
    Falls back to pitchedCommonName cleaning if needed.
    """
    try:
        cs = harmony.chordSymbolFigureFromChord(chord_obj, includeChordType=True)
        if cs and cs[0]:
            return cs[0]
    except Exception as e:
        # music21 raises various exceptions for unrecognized chord structures
        logger.debug("chordSymbolFigureFromChord failed for %s: %s", chord_obj, e)

    root = chord_obj.root()
    if root is None:
        return chord_obj.pitchedCommonName

    root_name = root.name.replace('-', 'b')  # music21 uses '-' for flat
    quality = chord_obj.quality

    quality_map = {
        'major': '',
        'minor': 'm',
        'diminished': 'dim',
        'augmented': 'aug',
        'dominant-seventh': '7',
        'major-seventh': 'maj7',
        'minor-seventh': 'm7',
        'half-diminished-seventh': 'm7b5',
        'diminished-seventh': 'dim7',
        'minor-major-seventh': 'mMaj7',
    }

    suffix = quality_map.get(quality, '')

    # Check for specific intervals if quality is 'other'
    if quality == 'other':
        pitches = [p.midi for p in chord_obj.pitches]
        if len(pitches) == 1:
            return root_name
        if len(pitches) == 2:
            interval = abs(pitches[1] - pitches[0]) % 12
            interval_names = {
                7: '5', 5: 'sus4', 2: 'sus2', 3: 'm(no5)', 4: '(no5)',
            }
            suffix = interval_names.get(interval, '')
        # Quartal trichords (stacked 4ths) -> treat as sus4
        raw_name = chord_obj.pitchedCommonName
        if 'quartal' in raw_name:
            suffix = 'sus4'
        elif 'whole-tone' in raw_name:
            suffix = 'sus2'

    return f"{root_name}{suffix}"


def detect_onset_order(pm_notes, start_time, window_ms=40):
    window = window_ms / 1000.0
    cluster = [n for n in pm_notes if abs(n.start - start_time) < window]
    if len(cluster) < 2:
        return "simultaneous"
    sorted_by_onset = sorted(cluster, key=lambda n: n.start)
    pitches_in_order = [n.pitch for n in sorted_by_onset]
    if pitches_in_order == sorted(pitches_in_order):
        return "ascending"
    elif pitches_in_order == sorted(pitches_in_order, reverse=True):
        return "descending"
    return "simultaneous"


def infer_direction(beat_position, onset_order, time_sig_numerator):
    if onset_order == "ascending":
        return "down"
    if onset_order == "descending":
        return "up"
    frac = beat_position % 1.0
    return "down" if frac < 0.05 else "up"


def extract(midi_path, override_bpm=None):
    score = converter.parse(midi_path)
    chords_stream = score.chordify()

    # Extract global metadata
    ts_obj = score.recurse().getElementsByClass(meter.TimeSignature).first()
    bpm_obj = score.recurse().getElementsByClass(tempo.MetronomeMark).first()
    ts_str = f"{ts_obj.numerator}/{ts_obj.denominator}" if ts_obj else "4/4"
    bpm = override_bpm if override_bpm else (int(bpm_obj.number) if bpm_obj else 120)
    ts_num = int(ts_str.split('/')[0])

    # Load pretty_midi for velocity/onset data
    pm = pretty_midi.PrettyMIDI(midi_path)
    all_pm_notes = [n for inst in pm.instruments for n in inst.notes]

    key_obj = score.analyze('key')
    key_str = f"{key_obj.tonic.name.replace('-', 'b')} {key_obj.mode}"

    measures_data = []
    m21_measures = list(chords_stream.recurse().getElementsByClass('Measure'))

    for m21_measure in m21_measures:
        measure_num = m21_measure.number
        measure_abs_offset = float(m21_measure.offset)
        chords_in_measure = list(m21_measure.getElementsByClass('Chord'))

        if not chords_in_measure:
            # Empty measure -> rest
            measures_data.append({
                "measure_number": measure_num,
                "chord_name": "rest",
                "chord_root": None,
                "chord_quality": "rest",
                "duration_beats": ts_num,
                "avg_velocity": 0,
                "voicing": [None, None, None, None, None, None],
                "beats": [{
                    "beat_position": 1.0,
                    "direction": "down",
                    "duration": "whole",
                    "duration_gp_value": 1,
                    "velocity": 0,
                    "onset_order": "simultaneous",
                    "is_rest": True
                }]
            })
            continue

        # Group consecutive chords with the same simplified name
        groups = []
        for chord_obj in chords_in_measure:
            chord_name = simplify_chord_name(chord_obj)
            chord_offset = float(chord_obj.offset)  # relative to measure
            ql = float(chord_obj.duration.quarterLength)
            gp_val, dur_name = quantize_duration(ql)

            root = chord_obj.root()
            root_name = root.name.replace('-', 'b') if root else "C"
            quality = chord_obj.quality
            avg_vel = int(sum(n.volume.velocity or 80 for n in chord_obj.notes)
                          / len(chord_obj.notes))

            # Compute onset time in seconds for strum detection
            abs_offset = measure_abs_offset + chord_offset
            onset_sec = pm.tick_to_time(
                int(abs_offset * pm.resolution)
            ) if pm.resolution else abs_offset * (60.0 / bpm)
            onset_order = detect_onset_order(all_pm_notes, onset_sec)

            # beat_position is 1-indexed (beat 1 = position 1.0)
            beat_pos = round(chord_offset + 1.0, 3)
            direction = infer_direction(chord_offset, onset_order, ts_num)

            beat_entry = {
                "beat_position": beat_pos,
                "direction": direction,
                "duration": dur_name,
                "duration_gp_value": gp_val,
                "velocity": avg_vel,
                "onset_order": onset_order
            }

            if groups and groups[-1]["chord_name"] == chord_name:
                groups[-1]["beats"].append(beat_entry)
                groups[-1]["duration_beats"] = round(
                    groups[-1]["duration_beats"] + ql, 3)
            else:
                groups.append({
                    "measure_number": measure_num,
                    "chord_name": chord_name,
                    "chord_root": root_name,
                    "chord_quality": quality,
                    "duration_beats": round(ql, 3),
                    "avg_velocity": avg_vel,
                    "voicing": [None, None, None, None, None, None],
                    "beats": [beat_entry]
                })

        measures_data.extend(groups)

    result = {
        "title": Path(midi_path).stem,
        "tempo": bpm,
        "time_signature": ts_str,
        "key": key_str,
        "measures": measures_data
    }

    out_path = midi_path.rsplit('.', 1)[0] + '_song_data.json'
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {len(measures_data)} entries ({len(m21_measures)} measures) to {out_path}")
    return out_path


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python extract_chords.py input.mid [bpm_override]", file=sys.stderr)
        sys.exit(1)
    midi_path = sys.argv[1]
    bpm_override = int(sys.argv[2]) if len(sys.argv) > 2 else None
    extract(midi_path, bpm_override)
