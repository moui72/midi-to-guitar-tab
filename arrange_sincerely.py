"""
Hand-arranged guitar tab for 'Sincerely' - MIDI to GP5 with voicing decisions by Claude.

Capo strategy:
  - No capo: M1-65 (D major + D minor sections)
  - Capo 1:  M66-74 (D#m modulated section, play same Dm shapes)

Voicing philosophy:
  - Favor open string 1 (E4) and string 2 (B3) where musically appropriate
  - Use Fmaj7 instead of F barre where possible
  - Use Bbmaj7 / Bb(add open E) instead of full Bb barre where possible
  - Arpeggios: preserve individual notes, assign to natural guitar strings
  - Block chords: use standard open shapes, optimize for voice leading

Usage: python arrange_sincerely.py Sincerely.mid output.gp5
"""

import sys
import csv
from pathlib import Path
from collections import defaultdict
from itertools import product
import pretty_midi
import guitarpro as gp

# ─── Tuning ────────────────────────────────────────────────────────────
STRING_MIDI = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40}  # EADGBE
MAX_FRET = 12

# ─── Duration quantization ────────────────────────────────────────────
DURATION_TABLE = [
    (4.0, 1, False), (3.0, 2, True), (2.0, 2, False),
    (1.5, 4, True), (1.0, 4, False), (0.75, 8, True),
    (0.5, 8, False), (0.375, 16, True), (0.25, 16, False),
    (0.125, 32, False),
]
GP_DUR_QL = {1: 4.0, 2: 2.0, 4: 1.0, 8: 0.5, 16: 0.25, 32: 0.125}


def quantize_ql(ql):
    best = min(DURATION_TABLE, key=lambda x: abs(x[0] - ql))
    return best[1], best[2], best[0]


# ─── Hand-picked voicings ─────────────────────────────────────────────
# Format: [s1(high E), s2(B), s3(G), s4(D), s5(A), s6(low E)]
# None = muted/not played
#
# NOTE: These are FRET numbers relative to capo (0 = open/capo position)

# Voicings favoring open strings 1+2
# Full 6-string voicings where possible; expand_voicing() fills gaps for dense MIDI
VOICINGS_OPEN = {
    # D major key
    "Em":     [0, 0, 0, 2, 2, 0],     # 6 strings, both E and B open
    "Em7":    [0, 0, 0, 0, 2, 0],     # 6 strings, both open
    "F#m":    [2, 2, 2, 4, 4, 2],     # 6 strings barre
    "G":      [3, 0, 0, 0, 2, 3],     # 6 strings, B open
    "Gmaj7":  [2, 0, 0, 0, 2, 3],     # 6 strings, B open
    "D":      [2, 3, 2, 0, 0, None],  # 5 strings, A bass = D/A
    "Dsus4":  [3, 3, 2, 0, 0, None],  # 5 strings
    "Dsus2":  [0, 3, 2, 0, 0, None],  # 5 strings, open E on top!
    "D/F#":   [2, 3, 2, 0, 0, 2],     # 6 strings
    "A":      [0, 2, 2, 2, 0, None],  # 5 strings, open E on top
    "Asus4":  [0, 3, 2, 2, 0, None],  # 5 strings, open E
    "Bm7":    [0, 0, 2, 0, 2, None],  # 5 strings, open E AND B
    "Bm":     [2, 3, 4, 4, 2, None],  # 5 strings barre
    "F#7":    [2, 2, 3, 2, 4, 2],     # 6 strings

    # D minor key - no capo
    "Dm":     [1, 3, 2, 0, 0, None],  # 5 strings, Dm/A
    "Dm7":    [1, 1, 2, 0, 0, None],  # 5 strings, Dm7/A
    "Am":     [0, 1, 2, 2, 0, 0],     # 6 strings, Am/E
    "Am7":    [0, 1, 0, 2, 0, 0],     # 6 strings, Am7/E
    "C":      [0, 1, 0, 2, 3, 0],     # 6 strings, C/E
    "C/E":    [0, 1, 0, 2, 2, 0],     # 6 strings
    "Cmaj7":  [0, 0, 0, 2, 3, 0],     # 6 strings, open E AND B
    "F":      [1, 1, 2, 3, 3, 1],     # 6 strings full barre
    "Fmaj7":  [0, 1, 2, 3, 3, 1],     # 6 strings, open E!
    "Fsus4":  [1, 1, 3, 3, 3, 1],     # 6 strings full barre
    "Bb":     [1, 3, 3, 3, 1, None],  # 5 strings barre
    "Bbmaj7": [1, 2, 3, 3, 1, None],  # 5 strings
    "Bb/D":   [1, 3, 3, 0, None, None],

    # Bridge / transition
    "E7":     [0, 0, 1, 0, 2, 0],     # 6 strings, open E and B
    "C#dim7": [2, 2, 1, 2, 4, None],  # 5 strings
    "E7/G#":  [0, 0, 1, 0, 2, 4],     # 6 strings
    "Csus4":  [1, 1, 0, 3, 3, None],  # 5 strings
}


def expand_voicing(voicing, chord_pitches, capo=0):
    """Fill None entries in voicing with chord tones from the MIDI cluster.
    Keeps chord quality intact -- only adds notes that belong to the chord."""
    if not any(v is None for v in voicing):
        return voicing

    # Get the chord's pitch classes from the MIDI
    chord_pcs = set(p % 12 for p in chord_pitches)
    result = list(voicing)

    for s_idx in range(6):
        if result[s_idx] is not None:
            continue
        s_num = s_idx + 1
        open_midi = STRING_MIDI[s_num] + capo
        # Try frets 0-5 for a chord tone on this string
        for fret in range(6):
            if (open_midi + fret) % 12 in chord_pcs:
                result[s_idx] = fret
                break

    return result

# Chord name normalization from MIDI analysis
CHORD_ALIASES = {
    "A#": "Bb", "A#maj7": "Bbmaj7", "A#m": "Bbm", "A#m7": "Bbm7",
    "D#m": "Ebm", "D#m7": "Ebm7",
    "F#sus4": "F#sus4", "F#": "F#",
    "C#": "C#", "C#dim7": "C#dim7",
    "Fsus4": "Fsus4", "Csus4": "Csus4",
    "Bb6": "Bbmaj7",  # close enough voicing
}

# For modulated section (capo 1), map actual chords to played shapes
CAPO1_SHAPE_MAP = {
    "Ebm": "Dm", "D#m": "Dm",
    "Bbm": "Am", "A#m": "Am",
    "F#": "Fmaj7", "F#sus4": "Fsus4",
    "C#": "C", "Db": "C",
    "Bb": "A", "A#": "A",
    "F#7": "F",
}


# ─── String assignment for arpeggios ──────────────────────────────────
def get_string_options(pitch, capo=0):
    options = []
    for s in range(1, 7):
        fret = pitch - (STRING_MIDI[s] + capo)
        if 0 <= fret <= MAX_FRET:
            options.append((s, fret))
    return options


def assign_single(pitch, capo=0):
    opts = get_string_options(pitch, capo)
    if not opts:
        return None
    opts.sort(key=lambda x: (x[1], x[0]))
    return opts[0]


def assign_cluster(pitches, capo=0):
    if len(pitches) == 1:
        r = assign_single(pitches[0], capo)
        return [r] if r else [None]

    all_opts = [get_string_options(p, capo) or [(None, None)] for p in pitches]

    best_combo = None
    best_score = float("inf")
    for combo in product(*all_opts):
        strings = [s for s, f in combo if s is not None]
        if len(strings) != len(set(strings)):
            continue
        frets = [f for s, f in combo if f is not None]
        if not frets:
            continue
        max_f = max(frets)
        non_zero = [f for f in frets if f > 0]
        spread = (max_f - min(non_zero)) if non_zero else 0
        avg = sum(frets) / len(frets)
        score = max_f * 10 + spread * 5 + avg
        if score < best_score:
            best_score = score
            best_combo = combo

    if best_combo:
        return list(best_combo)

    # Greedy fallback
    indexed = sorted(enumerate(pitches), key=lambda x: -x[1])
    used = set()
    result = [None] * len(pitches)
    for idx, p in indexed:
        opts = [(s, f) for s, f in get_string_options(p, capo) if s not in used]
        if opts:
            opts.sort(key=lambda x: (x[1], x[0]))
            result[idx] = opts[0]
            used.add(opts[0][0])
    return result


def voicing_to_assignments(voicing, capo=0):
    """Convert a voicing array to [(string, fret)] list for GP."""
    assignments = []
    for s_idx, fret in enumerate(voicing):
        if fret is not None:
            assignments.append((s_idx + 1, fret))
    return assignments


# ─── GP5 beat construction ────────────────────────────────────────────
def make_beat(voice, assignments, gp_val, dotted, vels=None, rest=False, let_ring=True):
    if rest or not assignments:
        b = gp.Beat(voice=voice, status=gp.BeatStatus.rest)
        b.duration = gp.Duration(value=gp_val, isDotted=dotted)
        return b

    b = gp.Beat(voice=voice, status=gp.BeatStatus.normal)
    b.duration = gp.Duration(value=gp_val, isDotted=dotted)
    for i, (s, f) in enumerate(assignments):
        v = vels[i] if vels and i < len(vels) else 80
        n = gp.Note(beat=b, value=f, string=s, velocity=min(127, max(1, v)))
        if let_ring:
            n.effect.letRing = True
        b.notes.append(n)
    return b


def fill_rest(voice, ql):
    beats = []
    r = ql
    for dur_ql, gp_val, dotted in DURATION_TABLE:
        while r >= dur_ql - 0.01:
            beats.append(make_beat(voice, [], gp_val, dotted, rest=True))
            r -= dur_ql
    return beats


# ─── Identify what chord a cluster belongs to ────────────────────────
def identify_chord(pitches):
    pcs = sorted(set(p % 12 for p in pitches))
    patterns = {
        (0,4,7): '', (0,3,7): 'm', (0,4,7,10): '7',
        (0,3,7,10): 'm7', (0,4,7,11): 'maj7', (0,3,6,9): 'dim7',
        (0,3,6,10): 'm7b5', (0,2,7): 'sus2', (0,5,7): 'sus4',
        (0,4,7,9): '6', (0,3,7,9): 'm6',
    }
    NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
    for pc in pcs:
        ints = tuple(sorted((p - pc) % 12 for p in pcs))
        if ints in patterns:
            return f'{NAMES[pc]}{patterns[ints]}'
    return None


# ─── Section detection ────────────────────────────────────────────────
def detect_sections(measure_events):
    """Auto-detect the modulated D#m section by finding D#m/A#m/F#/C# chords."""
    dsm_measures = set()
    NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
    patterns = {(0,4,7):'', (0,3,7):'m', (0,4,7,10):'7', (0,3,7,10):'m7',
        (0,4,7,11):'maj7', (0,3,6,9):'dim7', (0,2,7):'sus2', (0,5,7):'sus4'}

    for m_num, events in measure_events.items():
        for ev in events:
            if not ev["is_single"] and len(ev["pitches"]) >= 3:
                pcs = sorted(set(p % 12 for p in ev["pitches"]))
                for pc in pcs:
                    ints = tuple(sorted((p - pc) % 12 for p in pcs))
                    if ints in patterns:
                        name = f'{NAMES[pc]}{patterns[ints]}'
                        if name in ("D#m", "A#m", "F#sus4", "F#"):
                            dsm_measures.add(m_num)
                        break
    return dsm_measures


def get_section(measure, dsm_measures=None):
    if dsm_measures and measure in dsm_measures:
        return "modulated_Dsm"
    return "other"


def get_capo(measure, dsm_measures=None):
    if get_section(measure, dsm_measures) == "modulated_Dsm":
        return 1
    return 0


def get_played_chord(chord_name, measure, dsm_measures=None):
    """Map actual chord to played shape, considering capo."""
    name = CHORD_ALIASES.get(chord_name, chord_name)
    capo = get_capo(measure, dsm_measures)
    if capo == 1:
        name = CAPO1_SHAPE_MAP.get(chord_name, CAPO1_SHAPE_MAP.get(name, name))
    return name


def get_voicing_for_chord(played_chord):
    """Look up the hand-picked voicing for a played chord shape."""
    if played_chord in VOICINGS_OPEN:
        return VOICINGS_OPEN[played_chord]
    # Try without suffix variations
    for key in VOICINGS_OPEN:
        if played_chord.startswith(key):
            return VOICINGS_OPEN[key]
    return None


# ─── Main build ───────────────────────────────────────────────────────
def build(midi_path, output_path):
    pm = pretty_midi.PrettyMIDI(midi_path)
    notes = sorted(pm.instruments[0].notes, key=lambda n: n.start)

    # Use native MIDI tempo for beat positions
    tempo_changes = pm.get_tempo_changes()
    midi_bpm = tempo_changes[1][0] if len(tempo_changes[1]) > 0 else 120
    beat_dur_sec = 60.0 / midi_bpm

    # Compute total measures from note data
    last_time = max(n.end for n in notes)
    last_beat = last_time / beat_dur_sec
    total_measures = int(last_beat // 4) + 1

    # Cluster notes (within 0.05s = simultaneous)
    clusters = []
    i = 0
    while i < len(notes):
        start = notes[i].start
        cl = [notes[i]]
        j = i + 1
        while j < len(notes) and notes[j].start - start < 0.05:
            cl.append(notes[j])
            j += 1
        clusters.append(cl)
        i = j

    # Map to measures using beat positions
    measure_events = defaultdict(list)
    for cl in clusters:
        beat_abs = cl[0].start / beat_dur_sec
        m = int(beat_abs // 4) + 1
        bp = beat_abs % 4

        pitches = sorted(set(n.pitch for n in cl))
        vels = [n.velocity for n in cl]
        dur_beats = (cl[0].end - cl[0].start) / beat_dur_sec

        measure_events[m].append({
            "beat": round(bp * 4) / 4,  # snap to 16th grid
            "pitches": pitches,
            "vels": vels,
            "dur": dur_beats,
            "is_single": len(pitches) == 1,
        })

    # Auto-detect modulated D#m section and expand to continuous range
    dsm_measures = detect_sections(measure_events)
    if dsm_measures:
        dsm_start = min(dsm_measures)
        dsm_end = max(dsm_measures)
        dsm_measures = set(range(dsm_start, dsm_end + 1))
    print(f"D#m section: M{min(dsm_measures)}-M{max(dsm_measures)}" if dsm_measures else "No D#m modulation detected")

    # Create song with two tracks for capo change
    song = gp.Song()
    song.title = Path(midi_path).stem
    song.tempo = 72

    # Track 1: no capo (main sections)
    track1 = song.tracks[0]
    track1.name = "Acoustic Guitar"
    track1.channel.instrument = 25
    track1.offset = 0
    track1.strings = [gp.GuitarString(number=i, value=STRING_MIDI[i]) for i in range(1, 7)]

    # Track 2: capo 1 (modulated D#m section)
    track2 = gp.Track(song)
    track2.number = 2
    track2.name = "Acoustic Guitar (Capo 1)"
    track2.offset = 1  # capo at fret 1
    track2.isPercussionTrack = False
    track2.strings = [gp.GuitarString(number=i, value=STRING_MIDI[i]) for i in range(1, 7)]
    # Assign a different MIDI channel to avoid conflict
    track2.channel.channel = 1
    track2.channel.effectChannel = 2
    track2.channel.instrument = 25
    song.tracks.append(track2)

    song.measureHeaders[0].timeSignature.numerator = 4
    song.measureHeaders[0].timeSignature.denominator.value = 4
    # Add first measure to track2
    track2.measures.append(gp.Measure(track2, song.measureHeaders[0]))

    for m_num in range(2, total_measures + 1):
        h = gp.MeasureHeader()
        h.number = m_num
        h.timeSignature.numerator = 4
        h.timeSignature.denominator.value = 4
        song.measureHeaders.append(h)
        for t in song.tracks:
            t.measures.append(gp.Measure(t, h))

    # Build each measure - route to correct track based on section
    for m_num in range(1, total_measures + 1):
        is_dsm = m_num in dsm_measures
        capo = 1 if is_dsm else 0
        active_track = track2 if is_dsm else track1
        silent_track = track1 if is_dsm else track2

        # Silent track gets a whole rest
        silent_m = silent_track.measures[m_num - 1]
        silent_v = silent_m.voices[0]
        silent_v.beats.clear()
        silent_v.beats.append(make_beat(silent_v, [], 1, False, rest=True))

        measure = active_track.measures[m_num - 1]
        voice = measure.voices[0]
        voice.beats.clear()

        events = measure_events.get(m_num, [])
        if not events:
            voice.beats.append(make_beat(voice, [], 1, False, rest=True))
            continue

        events.sort(key=lambda e: e["beat"])

        # Snap to 16th grid and build, merging clusters that snap to same position
        grid = {}
        for ev in events:
            gp_pos = min(ev["beat"], 3.75)
            gp_pos = round(gp_pos * 4) / 4
            if gp_pos in grid:
                # Merge: combine pitches and velocities
                existing = grid[gp_pos]
                merged_pitches = sorted(set(existing["pitches"] + ev["pitches"]))
                merged_vels = existing["vels"] + ev["vels"]
                grid[gp_pos] = {
                    "beat": gp_pos,
                    "pitches": merged_pitches,
                    "vels": merged_vels[:len(merged_pitches)],
                    "dur": max(existing["dur"], ev["dur"]),
                    "is_single": len(merged_pitches) == 1,
                }
            else:
                grid[gp_pos] = ev

        sorted_pos = sorted(grid.keys())
        running = 0.0

        for i, pos in enumerate(sorted_pos):
            ev = grid[pos]

            # Rest gap
            gap = pos - running
            if gap >= 0.12:
                for rb in fill_rest(voice, gap):
                    voice.beats.append(rb)
                running = pos

            # Duration to next event or end
            if i + 1 < len(sorted_pos):
                dur = sorted_pos[i + 1] - pos
            else:
                dur = 4.0 - pos
            dur = max(0.25, dur)

            gp_val, dotted, actual = quantize_ql(dur)
            if actual > dur + 0.01:
                smaller = [(q, g, d) for q, g, d in DURATION_TABLE if q <= dur + 0.01]
                if smaller:
                    actual, gp_val, dotted = max(smaller, key=lambda x: x[0])
                else:
                    gp_val, dotted, actual = 16, False, 0.25

            pitches = ev["pitches"]
            vels = ev["vels"]

            if ev["is_single"]:
                # Arpeggio note: assign to best string
                result = assign_single(pitches[0], capo)
                if result:
                    voice.beats.append(make_beat(voice, [result], gp_val, dotted, vels))
                else:
                    voice.beats.append(make_beat(voice, [], gp_val, dotted, rest=True))
            else:
                # Chord: try hand-picked voicing first
                chord_name = identify_chord(pitches)
                played = get_played_chord(chord_name, m_num, dsm_measures) if chord_name else None
                voicing = get_voicing_for_chord(played) if played else None

                if voicing:
                    # Expand voicing to fill all strings with chord tones
                    full_voicing = expand_voicing(voicing, pitches, capo)
                    assigns = voicing_to_assignments(full_voicing, capo)
                    avg_vel = sum(vels) // len(vels)
                    v_list = [avg_vel] * len(assigns)
                    voice.beats.append(make_beat(voice, assigns, gp_val, dotted, v_list))
                else:
                    # Fallback: compute from pitches
                    assigns = assign_cluster(pitches, capo)
                    valid = [(s, f, v) for (s, f), v in zip(assigns, vels) if s is not None]
                    if valid:
                        pairs = [(s, f) for s, f, _ in valid]
                        vs = [v for _, _, v in valid]
                        voice.beats.append(make_beat(voice, pairs, gp_val, dotted, vs))
                    else:
                        voice.beats.append(make_beat(voice, [], gp_val, dotted, rest=True))

            running = pos + actual

            # Fill slot remainder
            slot_rem = dur - actual
            if slot_rem >= 0.12:
                for rb in fill_rest(voice, slot_rem):
                    voice.beats.append(rb)
                running += slot_rem

        # Fill end
        rem = 4.0 - running
        if rem >= 0.12:
            for rb in fill_rest(voice, rem):
                voice.beats.append(rb)

        # Timing check
        total = sum(
            GP_DUR_QL.get(b.duration.value, 1.0) * (1.5 if b.duration.isDotted else 1.0)
            for b in voice.beats
        )
        if abs(total - 4.0) > 0.01:
            # Emergency 16th grid
            voice.beats.clear()
            grid_sorted = sorted(grid.items())
            gi = 0
            for slot in range(16):
                sp = slot * 0.25
                if gi < len(grid_sorted) and abs(grid_sorted[gi][0] - sp) < 0.13:
                    ev = grid_sorted[gi][1]
                    assigns = assign_cluster(ev["pitches"], capo)
                    valid = [(s, f, v) for (s, f), v in zip(assigns, ev["vels"]) if s is not None]
                    if valid:
                        pairs = [(s, f) for s, f, _ in valid]
                        vs = [v for _, _, v in valid]
                        voice.beats.append(make_beat(voice, pairs, 16, False, vs))
                    else:
                        voice.beats.append(make_beat(voice, [], 16, False, rest=True))
                    gi += 1
                else:
                    voice.beats.append(make_beat(voice, [], 16, False, rest=True))

        if not voice.beats:
            voice.beats.append(make_beat(voice, [], 1, False, rest=True))

    gp.write(song, output_path)

    # Summary across both tracks
    all_tracks = [track1, track2]
    note_count = sum(len(b.notes) for t in all_tracks for m in t.measures for b in m.voices[0].beats)
    timing_bad = 0
    for t in all_tracks:
        for m in t.measures:
            total = sum(GP_DUR_QL.get(b.duration.value, 1.0) * (1.5 if b.duration.isDotted else 1.0)
                        for b in m.voices[0].beats)
            if abs(total - 4.0) > 0.01:
                timing_bad += 1
    max_fret = max((n.value for t in all_tracks for m in t.measures for b in m.voices[0].beats for n in b.notes), default=0)
    dsm_range = f"M{min(dsm_measures)}-M{max(dsm_measures)}" if dsm_measures else "none"
    print(f"Wrote {total_measures} measures to {output_path}")
    print(f"  Track 1: Acoustic Guitar (no capo)")
    print(f"  Track 2: Acoustic Guitar (capo 1) for {dsm_range}")
    print(f"  Notes: {note_count}, Timing issues: {timing_bad}, Max fret: {max_fret}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python arrange_sincerely.py input.mid [output.gp5]", file=sys.stderr)
        sys.exit(1)
    midi = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "Sincerely.gp5"
    build(midi, out)
