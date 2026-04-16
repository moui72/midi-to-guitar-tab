"""
Stage 2: Enhance song_data.json with guitar voicings, cleaned chord names,
and refined strumming patterns.
Usage: python enhance_json.py song_data.json
"""

import sys
import json
import copy
import re

# Guitar voicings: [str1(high E), str2(B), str3(G), str4(D), str5(A), str6(low E)]
# None = muted/not played
VOICINGS = {
    # Open chords - D minor key
    "Dm":     [1, 3, 2, 0, None, None],
    "Dm7":    [1, 1, 2, 0, None, None],
    "Am":     [0, 1, 2, 2, 0, None],
    "Am7":    [0, 1, 0, 2, 0, None],
    "C":      [0, 1, 0, 2, 3, None],
    "C/E":    [0, 1, 0, 2, 2, 0],
    "F":      [1, 1, 2, 3, 3, 1],
    "Bb":     [1, 3, 3, 3, 1, None],
    "Bbmaj7": [1, 3, 2, 3, 1, None],
    "Bbsus2": [1, 1, 3, 3, 1, None],
    "G":      [3, 0, 0, 0, 2, 3],
    "Gsus2":  [3, 0, 0, 2, 0, 3],
    "Gsus4":  [3, 1, 0, 0, 2, 3],
    "Em":     [0, 0, 0, 2, 2, 0],
    "D":      [2, 3, 2, 0, None, None],
    "D/F#":   [2, 3, 2, 0, 0, 2],
    "D/A":    [2, 3, 2, 0, 0, None],
    "Dsus2":  [0, 3, 2, 0, None, None],
    "A":      [0, 2, 2, 2, 0, None],
    "A/C#":   [0, 2, 2, 2, 4, None],
    "Asus4":  [0, 3, 2, 2, 0, None],
    "Asus2":  [0, 0, 2, 2, 0, None],
    "Bm":     [2, 3, 4, 4, 2, None],
    "Bm7":    [2, 0, 2, 0, 2, None],
    "F#m":    [2, 2, 2, 4, 4, 2],
    "Fsus4":  [1, 1, 3, 3, 3, 1],
    "Csus4":  [1, 1, 0, 3, 3, None],
    "E7":     [0, 0, 1, 0, 2, 0],
    "E7/G#":  [0, 0, 1, 0, 2, 4],
    "C#dim7": [None, 2, 1, 2, None, None],

    # Coda section chords (modulation area)
    "Ebm":    [None, 4, 4, 3, 1, None],
    "Db":     [None, 4, 3, 1, 4, None],
    "Bsus2":  [2, 2, 4, 4, 2, None],
    "Bb/D":   [1, 3, 3, 3, None, None],
    "Gb":     [2, 2, 3, 4, 4, 2],
    "B":      [2, 4, 4, 4, 2, None],
    "C#m":    [4, 5, 6, 6, 4, None],
}


# Map messy chord names from music21 to clean guitar chord names
CHORD_NAME_MAP = {
    "Gpedal": "G",
    "Apedal": "A",
    "Bpedal": "Bm",
    "Dpedal": "D",
    "Epedal": "Em",
    "Fpedal": "F",
    "F#pedal": "D/F#",
    "B-pedal": "Bb",
    "Dpower/A": "D/A",
    "Dpower": "D",
    "Apower": "A",
    "ApoweraddG": "A",
    "B-power": "Bb",
    "B-poweraddA": "Bb",
    "B-poweraddC#": "Bb",
    "E-poweraddF#": "Ebm",
    "Bsus": "Bm",
    "Asus": "Asus4",
    "Fsus": "Fsus4",
    "Csus": "Csus4",
    "B-sus2": "Bbsus2",
    "B-maj7": "Bbmaj7",
    "B-": "Bb",
    "B-/D": "Bb/D",
    "Bm/DaddE": "Bm",
    "Eø7/D": "Em",
    "C#o7/G": "C#dim7",
    "F/C": "F",
}


def clean_chord_name(entry):
    """Resolve chord name from the raw music21 output."""
    name = entry["chord_name"]

    if name == "rest":
        return "rest"

    # Direct mapping
    if name in CHORD_NAME_MAP:
        return CHORD_NAME_MAP[name]

    # Already clean
    if name in VOICINGS:
        return name

    # "Chord Symbol Cannot Be Identified" -- use root + quality
    if "Cannot Be Identified" in name:
        root = entry.get("chord_root", "C")
        if root:
            root = root.replace("-", "b")
        quality = entry.get("chord_quality", "other")
        if quality == "major":
            return root
        elif quality == "minor":
            return f"{root}m"
        elif quality == "other":
            return root
        return root

    return name


def get_voicing(chord_name):
    """Look up voicing, with fallback."""
    if chord_name in VOICINGS:
        return VOICINGS[chord_name]
    # Try without bass note
    if "/" in chord_name:
        base = chord_name.split("/")[0]
        if base in VOICINGS:
            return VOICINGS[base]
    return None


def refine_strumming(beats, measure_position_in_section=0):
    """
    Apply 4/4 strumming rules at BPM 72 (slow, soulful):
    - Beat 1 and 3: down
    - Off-beats: up
    - High velocity (>100): force down
    - Low velocity (<50): force up
    """
    for beat in beats:
        if beat.get("is_rest"):
            continue

        bp = beat["beat_position"]
        vel = beat.get("velocity", 80)

        # Velocity overrides
        if vel > 100:
            beat["direction"] = "down"
            continue
        if vel < 50:
            beat["direction"] = "up"
            continue

        # Beat position rules (1-indexed)
        on_beat = bp in (1.0, 2.0, 3.0, 4.0)
        structural_beat = bp in (1.0, 3.0)

        if structural_beat:
            beat["direction"] = "down"
        elif on_beat:
            beat["direction"] = "down"
        else:
            # Off-beat (e.g. 1.5, 2.5, etc.)
            frac = bp % 1.0
            if frac > 0.01:
                beat["direction"] = "up"
            else:
                beat["direction"] = "down"

    return beats


def enhance(json_path):
    with open(json_path) as f:
        data = json.load(f)

    enhanced = copy.deepcopy(data)
    warnings = []

    for i, entry in enumerate(enhanced["measures"]):
        # Clean chord name
        old_name = entry["chord_name"]
        new_name = clean_chord_name(entry)
        entry["chord_name"] = new_name

        if new_name == "rest":
            continue

        # Update root
        if "/" in new_name:
            entry["chord_root"] = new_name.split("/")[0]
        else:
            base = re.sub(r'(maj7|m7b5|dim7|m7|sus[24]|aug|dim|m|7)$', '', new_name)
            if base:
                entry["chord_root"] = base

        # Assign voicing
        voicing = get_voicing(new_name)
        if voicing:
            entry["voicing"] = voicing
        else:
            warnings.append(
                f"M{entry['measure_number']}: no voicing for '{new_name}' (was '{old_name}')"
            )
            # Try to find closest match
            for candidate in VOICINGS:
                if candidate.startswith(entry.get("chord_root", "X")):
                    entry["voicing"] = VOICINGS[candidate]
                    entry["warning"] = f"Using {candidate} voicing as fallback for {new_name}"
                    break

        # Refine strumming
        entry["beats"] = refine_strumming(entry["beats"])

        # Check for impossible transitions
        if i > 0:
            prev = enhanced["measures"][i - 1]
            prev_voicing = prev.get("voicing", [])
            curr_voicing = entry.get("voicing", [])
            if prev_voicing and curr_voicing:
                prev_frets = [f for f in prev_voicing if f is not None]
                curr_frets = [f for f in curr_voicing if f is not None]
                if prev_frets and curr_frets:
                    max_prev = max(prev_frets)
                    max_curr = max(curr_frets)
                    if abs(max_prev - max_curr) > 7:
                        entry["warning"] = (
                            f"Large fret jump from previous chord "
                            f"(max fret {max_prev} -> {max_curr})"
                        )

    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  {w}")

    out_path = json_path.replace("_song_data.json", "_song_data_enhanced.json")
    with open(out_path, "w") as f:
        json.dump(enhanced, f, indent=2)
    print(f"\nWrote enhanced data to {out_path}")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python enhance_json.py song_data.json", file=sys.stderr)
        sys.exit(1)
    enhance(sys.argv[1])
