"""
Stage 3: song_data_enhanced.json -> output.gp5
Usage: python build_gp5.py song_data_enhanced.json [output.gp5]
"""

import sys
import json
from collections import defaultdict
import guitarpro as gp


# Standard EADGBE tuning: string number -> MIDI pitch
STANDARD_TUNING_MIDI = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40}

# Duration value -> quarterLength for fill calculation
GP_DUR_TO_QL = {1: 4.0, 2: 2.0, 4: 1.0, 8: 0.5, 16: 0.25, 32: 0.125}


def make_rest_beat(voice, gp_value=4):
    """Create a rest beat with the given GP duration value."""
    beat = gp.Beat(voice=voice, status=gp.BeatStatus.rest)
    beat.duration = gp.Duration(value=gp_value)
    return beat


def make_chord_beat(voice, voicing, gp_value, direction_str, velocity):
    """Create a chord beat with notes from voicing."""
    beat = gp.Beat(voice=voice, status=gp.BeatStatus.normal)
    beat.duration = gp.Duration(value=gp_value)

    # Strum direction
    if direction_str == "down":
        stroke_dir = gp.BeatStrokeDirection.down
    else:
        stroke_dir = gp.BeatStrokeDirection.up
    beat.effect.stroke = gp.BeatStroke(direction=stroke_dir, value=8)

    # Notes from voicing
    # voicing[0] = string 1 (high E), voicing[5] = string 6 (low E)
    vel = min(127, max(1, velocity))
    for str_idx, fret in enumerate(voicing):
        if fret is None:
            continue
        string_num = str_idx + 1
        note = gp.Note(
            beat=beat,
            value=fret,
            string=string_num,
            velocity=vel,
        )
        beat.notes.append(note)

    return beat


def fill_measure_to_full(voice, beats_so_far, ts_num, ts_den):
    """
    If beats don't fill the full measure, add rest beats.
    Returns the list of all beats for the measure.
    """
    total_ql_needed = ts_num * (4.0 / ts_den)  # e.g. 4/4 -> 4.0 ql
    current_ql = sum(GP_DUR_TO_QL.get(b.duration.value, 1.0) for b in beats_so_far)

    remaining = total_ql_needed - current_ql
    result = list(beats_so_far)

    # Fill remaining with appropriate rest durations
    while remaining > 0.01:
        if remaining >= 4.0:
            result.append(make_rest_beat(voice, 1))
            remaining -= 4.0
        elif remaining >= 2.0:
            result.append(make_rest_beat(voice, 2))
            remaining -= 2.0
        elif remaining >= 1.0:
            result.append(make_rest_beat(voice, 4))
            remaining -= 1.0
        elif remaining >= 0.5:
            result.append(make_rest_beat(voice, 8))
            remaining -= 0.5
        elif remaining >= 0.25:
            result.append(make_rest_beat(voice, 16))
            remaining -= 0.25
        else:
            break

    return result


def build_gp5(json_path, output_path=None):
    with open(json_path) as f:
        data = json.load(f)

    # Group entries by measure number
    measures_by_num = defaultdict(list)
    for entry in data["measures"]:
        measures_by_num[entry["measure_number"]].append(entry)

    if not measures_by_num:
        print("No measures found.")
        return

    max_measure = max(measures_by_num.keys())

    # Parse time signature
    ts_str = data.get("time_signature", "4/4")
    ts_num, ts_den = map(int, ts_str.split("/"))

    # Create song
    song = gp.Song()
    song.title = data.get("title", "Untitled")
    song.tempo = data.get("tempo", 120)

    # Configure acoustic guitar track
    track = song.tracks[0]
    track.name = "Acoustic Guitar"
    track.isPercussionTrack = False
    track.channel.instrument = 25  # Acoustic Guitar (steel)
    track.strings = [
        gp.GuitarString(number=i, value=STANDARD_TUNING_MIDI[i])
        for i in range(1, 7)
    ]

    # The default song has 1 measure header and 1 measure per track.
    # We need max_measure total.
    # First, configure the existing header
    song.measureHeaders[0].timeSignature.numerator = ts_num
    song.measureHeaders[0].timeSignature.denominator.value = ts_den

    # Add remaining measure headers
    for m_num in range(2, max_measure + 1):
        header = gp.MeasureHeader()
        header.number = m_num
        header.timeSignature.numerator = ts_num
        header.timeSignature.denominator.value = ts_den
        # tempo is set on the Song object, not per-header in this version
        song.measureHeaders.append(header)
        for t in song.tracks:
            measure = gp.Measure(t, header)
            t.measures.append(measure)

    # Populate measures
    for m_num in range(1, max_measure + 1):
        measure = track.measures[m_num - 1]
        voice = measure.voices[0]
        voice.beats.clear()

        entries = measures_by_num.get(m_num, [])
        beats_for_measure = []

        if not entries or (len(entries) == 1 and entries[0]["chord_name"] == "rest"):
            # Whole measure rest
            voice.beats.append(make_rest_beat(voice, 1))
            continue

        for entry in entries:
            chord_name = entry["chord_name"]
            voicing = entry.get("voicing", [None] * 6)
            has_voicing = any(v is not None for v in voicing)

            for beat_data in entry.get("beats", []):
                gp_val = beat_data.get("duration_gp_value", 4)
                direction = beat_data.get("direction", "down")
                vel = beat_data.get("velocity", 80)
                is_rest = beat_data.get("is_rest", False)

                if is_rest or chord_name == "rest" or not has_voicing:
                    beats_for_measure.append(make_rest_beat(voice, gp_val))
                else:
                    beats_for_measure.append(
                        make_chord_beat(voice, voicing, gp_val, direction, vel)
                    )

        # Fill any remaining time in the measure with rests
        all_beats = fill_measure_to_full(voice, beats_for_measure, ts_num, ts_den)

        # Truncate if we somehow exceed the measure
        total_ql_target = ts_num * (4.0 / ts_den)
        final_beats = []
        running_ql = 0.0
        for b in all_beats:
            b_ql = GP_DUR_TO_QL.get(b.duration.value, 1.0)
            if running_ql + b_ql > total_ql_target + 0.01:
                break
            final_beats.append(b)
            running_ql += b_ql

        if not final_beats:
            final_beats = [make_rest_beat(voice, 1)]

        for b in final_beats:
            voice.beats.append(b)

    # Write output
    if output_path is None:
        output_path = json_path.replace("_enhanced.json", ".gp5").replace(".json", ".gp5")

    gp.write(song, output_path)
    print(f"Wrote {max_measure} measures to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python build_gp5.py song_data_enhanced.json [output.gp5]", file=sys.stderr)
        sys.exit(1)
    json_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    build_gp5(json_path, out_path)
