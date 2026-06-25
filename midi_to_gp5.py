"""
Direct MIDI -> GP5 conversion preserving original rhythm, arpeggios, and note durations.
No chordify. Maps each MIDI note to guitar string/fret with smart voicing.

Usage: python midi_to_gp5.py input.mid [output.gp5] [bpm] [--bass]
"""

import sys
import argparse
from itertools import product
from pathlib import Path
from collections import defaultdict
import pretty_midi
import guitarpro as gp


# String tuning presets: string number (1=highest pitch) -> open MIDI pitch
GUITAR_STRINGS = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40}  # EADGBE
BASS_STRINGS   = {1: 43, 2: 38, 3: 33, 4: 28}                 # GDAE (4-string standard)

MAX_FRET = 15

# GP duration quantization table
DURATION_TABLE = [
    (4.0, 1, False),
    (3.0, 2, True),
    (2.0, 2, False),
    (1.5, 4, True),
    (1.0, 4, False),
    (0.75, 8, True),
    (0.5, 8, False),
    (0.375, 16, True),
    (0.25, 16, False),
    (0.125, 32, False),
]

GP_DUR_TO_QL = {1: 4.0, 2: 2.0, 4: 1.0, 8: 0.5, 16: 0.25, 32: 0.125}


def quantize_ql(ql):
    """Snap quarterLength to nearest GP duration. Returns (gp_value, is_dotted, actual_ql)."""
    best = min(DURATION_TABLE, key=lambda x: abs(x[0] - ql))
    return best[1], best[2], best[0]


def get_string_options(pitch, string_midi):
    """Get all valid (string, fret) for a MIDI pitch, sorted by preference."""
    options = []
    for s_num in string_midi:
        fret = pitch - string_midi[s_num]
        if 0 <= fret <= MAX_FRET:
            options.append((s_num, fret))
    return options


def assign_single_note(pitch, string_midi):
    """Assign a single note to the best string/fret."""
    options = get_string_options(pitch, string_midi)
    if not options:
        return None
    # Prefer: fret 0-5 on natural string, then lowest fret overall
    options.sort(key=lambda x: (x[1], x[0]))
    return options[0]


def assign_cluster(pitches, string_midi):
    """
    Assign multiple simultaneous notes to guitar strings.
    Uses brute-force search to minimize fret spread.
    Returns list of (string, fret) matching input order, or None for unplaceable notes.
    """
    if len(pitches) == 1:
        result = assign_single_note(pitches[0], string_midi)
        return [result] if result else [(None, None)]

    # Get options for each note
    all_options = [get_string_options(p, string_midi) for p in pitches]

    # If any note has no options, mark it None
    for i, opts in enumerate(all_options):
        if not opts:
            all_options[i] = [(None, None)]

    # Brute force: try all combinations, pick best valid one
    best_combo = None
    best_score = float("inf")

    for combo in product(*all_options):
        # Check: no two notes on same string (skip None strings)
        strings_used = [s for s, f in combo if s is not None]
        if len(strings_used) != len(set(strings_used)):
            continue

        # Score: prefer lower positions, penalize high frets heavily
        frets = [f for s, f in combo if f is not None]
        if not frets:
            continue

        max_fret = max(frets)
        non_zero = [f for f in frets if f > 0]
        fret_spread = (max_fret - min(non_zero)) if non_zero else 0
        avg_fret = sum(frets) / len(frets)
        score = max_fret * 10 + fret_spread * 5 + avg_fret

        if score < best_score:
            best_score = score
            best_combo = combo

    if best_combo is None:
        # Fallback: assign greedily from highest pitch down
        return _greedy_assign(pitches, string_midi)

    return list(best_combo)


def _greedy_assign(pitches, string_midi):
    """Greedy fallback: assign from highest pitch to lowest, taking best available string."""
    indexed = sorted(enumerate(pitches), key=lambda x: -x[1])
    used_strings = set()
    result = [(None, None)] * len(pitches)

    for orig_idx, pitch in indexed:
        options = get_string_options(pitch, string_midi)
        options = [(s, f) for s, f in options if s not in used_strings]
        if options:
            options.sort(key=lambda x: (x[1], x[0]))
            best = options[0]
            result[orig_idx] = best
            used_strings.add(best[0])

    return result


def make_beat(voice, note_assignments, gp_value, is_dotted, velocities=None, is_rest=False):
    """
    Create a GP beat.
    note_assignments: list of (string, fret) or None for skipped notes
    """
    valid_notes = [(s, f) for sf in [note_assignments] if sf for s, f in [sf] if s is not None] if not isinstance(note_assignments, list) else [(s, f) for s, f in note_assignments if s is not None and f is not None]

    if is_rest or not valid_notes:
        beat = gp.Beat(voice=voice, status=gp.BeatStatus.rest)
        beat.duration = gp.Duration(value=gp_value, isDotted=is_dotted)
        return beat

    beat = gp.Beat(voice=voice, status=gp.BeatStatus.normal)
    beat.duration = gp.Duration(value=gp_value, isDotted=is_dotted)

    for i, (string_num, fret) in enumerate(valid_notes):
        vel = velocities[i] if velocities and i < len(velocities) else 80
        note = gp.Note(
            beat=beat,
            value=fret,
            string=string_num,
            velocity=min(127, max(1, vel)),
        )
        note.effect.letRing = True
        beat.notes.append(note)

    return beat


def fill_remaining(voice, remaining_ql):
    """Add rest beats to fill remaining time in measure."""
    beats = []
    r = remaining_ql
    while r > 0.06:
        if r >= 4.0:
            beats.append(make_beat(voice, [], 1, False, is_rest=True))
            r -= 4.0
        elif r >= 3.0:
            beats.append(make_beat(voice, [], 2, True, is_rest=True))
            r -= 3.0
        elif r >= 2.0:
            beats.append(make_beat(voice, [], 2, False, is_rest=True))
            r -= 2.0
        elif r >= 1.5:
            beats.append(make_beat(voice, [], 4, True, is_rest=True))
            r -= 1.5
        elif r >= 1.0:
            beats.append(make_beat(voice, [], 4, False, is_rest=True))
            r -= 1.0
        elif r >= 0.75:
            beats.append(make_beat(voice, [], 8, True, is_rest=True))
            r -= 0.75
        elif r >= 0.5:
            beats.append(make_beat(voice, [], 8, False, is_rest=True))
            r -= 0.5
        elif r >= 0.25:
            beats.append(make_beat(voice, [], 16, False, is_rest=True))
            r -= 0.25
        else:
            break
    return beats


def build_gp5(midi_path, output_path, target_bpm=72, bass=False, pad_measures=0):
    pm = pretty_midi.PrettyMIDI(midi_path)
    string_midi = BASS_STRINGS if bass else GUITAR_STRINGS
    if not pm.instruments or not pm.instruments[0].notes:
        print("No notes found.")
        return

    seen = set()
    raw_notes = []
    for inst in pm.instruments:
        for n in inst.notes:
            key = (n.pitch, round(n.start, 3))
            if key not in seen:
                seen.add(key)
                raw_notes.append(n)
    raw_notes.sort(key=lambda n: n.start)
    tempo_changes = pm.get_tempo_changes()
    midi_bpm = tempo_changes[1][0] if len(tempo_changes[1]) > 0 else 120
    beat_dur = 60.0 / midi_bpm

    # Convert to beat-domain events
    events = []
    for n in raw_notes:
        events.append({
            "pitch": n.pitch,
            "velocity": n.velocity,
            "beat": n.start / beat_dur,
            "dur_ql": (n.end - n.start) / beat_dur,
        })

    last_beat = max(e["beat"] + e["dur_ql"] for e in events)
    total_measures = max(int(last_beat // 4) + 1, pad_measures)

    # Cluster notes by start time (within 0.06 beats = simultaneous)
    CLUSTER_THRESH = 0.06
    clusters = []
    i = 0
    sorted_events = sorted(events, key=lambda e: e["beat"])
    while i < len(sorted_events):
        cluster_start = sorted_events[i]["beat"]
        cluster = [sorted_events[i]]
        j = i + 1
        while j < len(sorted_events) and sorted_events[j]["beat"] - cluster_start < CLUSTER_THRESH:
            cluster.append(sorted_events[j])
            j += 1
        clusters.append({
            "beat": cluster_start,
            "notes": cluster,
        })
        i = j

    print(f"Notes: {len(events)}, Clusters: {len(clusters)}, Measures: {total_measures}")

    # Group by measure
    measure_clusters = defaultdict(list)
    for cl in clusters:
        m_num = int(cl["beat"] // 4) + 1
        beat_in_m = cl["beat"] % 4  # 0-indexed
        measure_clusters[m_num].append((beat_in_m, cl))

    # Create GP song
    song = gp.Song()
    song.title = Path(midi_path).stem
    song.tempo = target_bpm

    track = song.tracks[0]
    track.name = "Bass Guitar" if bass else "Acoustic Guitar"
    track.isPercussionTrack = False
    track.channel.instrument = 34 if bass else 25
    track.strings = [gp.GuitarString(number=i, value=string_midi[i]) for i in sorted(string_midi)]

    song.measureHeaders[0].timeSignature.numerator = 4
    song.measureHeaders[0].timeSignature.denominator.value = 4

    for m_num in range(2, total_measures + 1):
        header = gp.MeasureHeader()
        header.number = m_num
        header.timeSignature.numerator = 4
        header.timeSignature.denominator.value = 4
        song.measureHeaders.append(header)
        for t in song.tracks:
            t.measures.append(gp.Measure(t, header))

    # Build each measure using sixteenth-grid approach for perfect timing
    # Quantize all cluster positions to nearest sixteenth note (0.25 beat grid)
    for m_num in range(1, total_measures + 1):
        measure = track.measures[m_num - 1]
        voice = measure.voices[0]
        voice.beats.clear()

        m_clusters = measure_clusters.get(m_num, [])
        if not m_clusters:
            voice.beats.append(make_beat(voice, [], 1, False, is_rest=True))
            continue

        m_clusters.sort(key=lambda x: x[0])

        # Snap cluster positions to sixteenth grid
        grid = {}  # grid_pos -> (cluster_notes, velocities)
        for beat_pos, cl in m_clusters:
            grid_pos = round(beat_pos * 4) / 4  # snap to nearest 0.25
            grid_pos = min(grid_pos, 3.75)  # clamp to last sixteenth of measure
            pitches = [ne["pitch"] for ne in cl["notes"]]
            vels = [ne["velocity"] for ne in cl["notes"]]
            if grid_pos in grid:
                # Merge with existing cluster at this grid position
                grid[grid_pos] = (grid[grid_pos][0] + pitches, grid[grid_pos][1] + vels)
            else:
                grid[grid_pos] = (pitches, vels)

        # Build beats: walk through grid positions and assign durations
        sorted_positions = sorted(grid.keys())
        beat_list = []  # (grid_pos, duration_ql, pitches, vels)

        for i, pos in enumerate(sorted_positions):
            pitches, vels = grid[pos]
            # Duration = gap to next event (or end of measure)
            if i + 1 < len(sorted_positions):
                raw_dur = sorted_positions[i + 1] - pos
            else:
                raw_dur = 4.0 - pos
            raw_dur = max(0.25, raw_dur)
            beat_list.append((pos, raw_dur, pitches, vels))

        # Now emit beats with rests for gaps
        running_ql = 0.0
        for pos, dur, pitches, vels in beat_list:
            # Fill gap before this event
            gap = pos - running_ql
            if gap >= 0.12:
                for rb in fill_remaining(voice, gap):
                    voice.beats.append(rb)
                running_ql = pos

            # Emit the note beat + any rest beats to fill the duration
            assignments = assign_cluster(pitches, string_midi)
            valid = [(s, f, v) for (s, f), v in zip(assignments, vels) if s is not None]

            # Find the largest GP duration that fits within dur
            gp_val, is_dotted, actual_ql = quantize_ql(dur)
            if actual_ql > dur + 0.01:
                # Quantized duration exceeds available space; use next smaller
                smaller = [(ql, gv, dot) for ql, gv, dot in DURATION_TABLE if ql <= dur + 0.01]
                if smaller:
                    actual_ql, gp_val, is_dotted = max(smaller, key=lambda x: x[0])
                else:
                    gp_val, is_dotted, actual_ql = 16, False, 0.25

            if valid:
                note_pairs = [(s, f) for s, f, _ in valid]
                note_vels = [v for _, _, v in valid]
                voice.beats.append(make_beat(voice, note_pairs, gp_val, is_dotted, note_vels))
            else:
                voice.beats.append(make_beat(voice, [], gp_val, is_dotted, is_rest=True))
            running_ql = pos + actual_ql

            # Fill remainder of this beat's slot with rests
            slot_remaining = dur - actual_ql
            if slot_remaining >= 0.12:
                for rb in fill_remaining(voice, slot_remaining):
                    voice.beats.append(rb)
                running_ql += slot_remaining

        # Fill remaining time in measure
        remaining = 4.0 - running_ql
        if remaining >= 0.12:
            for rb in fill_remaining(voice, remaining):
                voice.beats.append(rb)

        # Final timing check
        total_ql = sum(
            GP_DUR_TO_QL.get(b.duration.value, 1.0) * (1.5 if b.duration.isDotted else 1.0)
            for b in voice.beats
        )
        if abs(total_ql - 4.0) > 0.01:
            # Emergency: rebuild as pure sixteenth grid
            voice.beats.clear()
            grid_sorted = sorted(grid.items())
            grid_idx = 0
            for slot_num in range(16):
                slot_pos = slot_num * 0.25
                if grid_idx < len(grid_sorted) and abs(grid_sorted[grid_idx][0] - slot_pos) < 0.13:
                    pitches, vels = grid_sorted[grid_idx][1]
                    assignments = assign_cluster(pitches, string_midi)
                    valid = [(s, f, v) for (s, f), v in zip(assignments, vels) if s is not None]
                    if valid:
                        note_pairs = [(s, f) for s, f, _ in valid]
                        note_vels = [v for _, _, v in valid]
                        voice.beats.append(make_beat(voice, note_pairs, 16, False, note_vels))
                    else:
                        voice.beats.append(make_beat(voice, [], 16, False, is_rest=True))
                    grid_idx += 1
                else:
                    voice.beats.append(make_beat(voice, [], 16, False, is_rest=True))

        if not voice.beats:
            voice.beats.append(make_beat(voice, [], 1, False, is_rest=True))

    content_measures = int(last_beat // 4) + 1
    gp.write(song, output_path)
    if total_measures > content_measures:
        print(f"Wrote {total_measures} measures to {output_path} ({total_measures - content_measures} empty bars padded)")
    else:
        print(f"Wrote {total_measures} measures to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert MIDI to GP5 tab.",
        usage="%(prog)s input.mid [output.gp5] [bpm] [--bass]",
    )
    parser.add_argument("input", help="Input MIDI file")
    parser.add_argument("output", nargs="?", default="output.gp5", help="Output GP5 file (default: output.gp5)")
    parser.add_argument("bpm", nargs="?", type=int, default=72, help="Playback BPM (default: 72)")
    parser.add_argument("--bass", action="store_true", help="Bass guitar mode: 4-string standard GDAE tuning")
    parser.add_argument("--measures", type=int, default=0, metavar="N", help="Pad output to at least N measures with empty bars")
    args = parser.parse_args()
    build_gp5(args.input, args.output, args.bpm, bass=args.bass, pad_measures=args.measures)
