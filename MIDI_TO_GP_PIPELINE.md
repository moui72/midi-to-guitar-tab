# MIDI → Acoustic Guitar Tab Pipeline
## Claude Code Context & Implementation Spec

---

## What This Project Does

Converts a **DAW-exported, quantized keyboard MIDI file** into a playable **acoustic guitar chord+strumming tab** in Guitar Pro 5 format (`.gp5`), using a three-stage Python pipeline. The output is a reasonable first-draft tab that the user fine-tunes manually in Guitar Pro.

**The core translation problem**: keyboard MIDI contains polyphonic voicings that are physically impossible or awkward on guitar. The pipeline must:
1. Detect chords from simultaneous MIDI notes
2. Re-voice them into playable 6-string guitar positions
3. Infer strumming direction (down/up) from MIDI velocity and timing
4. Render everything into a `.gp5` file with correct strokes, durations, and fret positions

---

## Input / Output Contract

- **Input**: any `.mid` file, 4/4 or 3/4 or 6/8 time, exported from a DAW by a keyboard player (notes are quantized and clean, no sloppy timing)
- **Output**: `output.gp5` — Guitar Pro 5 file, single acoustic guitar track, standard EADGBE tuning, with chord diagrams, strum direction markers, and correct note durations
- **Target quality**: equivalent to a competent guitarist's first-draft sketch — correct chords, plausible strumming, needs manual polish for dynamics and voicing flow

---

## Library Stack

Install everything before starting:

```bash
pip install music21 pretty-midi pychord PyGuitarPro
pip install git+https://github.com/bspaans/python-mingus
```

| Library | Version | Role | Key caveat |
|---|---|---|---|
| `music21` | v9.x | MIDI parsing, chord detection via `chordify()` | Slow on dense MIDI; use `recurse()` carefully |
| `pretty_midi` | v0.2.11 | Per-note velocity + timing extraction | No chord detection; used alongside music21 |
| `pychord` | v1.3.2 | Chord name → note set, handles inversions/slash chords | Lightweight fallback for chord ID |
| `mingus` | v0.6.x (GitHub) | Chord → guitar fret positions via `find_chord_fingering()` | **Must install from GitHub, not PyPI** |
| `PyGuitarPro` | v0.10.1 | Reads/writes GP3/4/5 format | Does NOT support GPX/GP7; GP5 opens fine in GP7/8 |

---

## Architecture: Three Stages

```
Stage 1 — extract_chords.py  (pure Python/music21)
  .mid file
  → music21.converter.parse()
  → score.chordify()           # collapse all tracks into chord stream
  → per-beat: chord name, root, inversion, duration, avg velocity
  → pretty_midi: per-note onset order within chord clusters (strum direction hint)
  → beat-position heuristics: on-beat = down, off-beat = up
  → output: song_data.json

Stage 2 — Claude reviews song_data.json  (LLM judgment)
  Claude reads the JSON and:
  - Selects best guitar voicing per chord (open vs barre, capo suggestion)
  - Refines strumming pattern to match time signature and genre feel
  - Adds extensions (sus4, add9) where musically appropriate
  - Flags any chord it can't map to guitar cleanly
  → output: song_data_enhanced.json  (same schema, updated fields)

Stage 3 — build_gp5.py  (pure Python/PyGuitarPro)
  song_data_enhanced.json
  → mingus.find_chord_fingering() for each chord → pick best voicing
  → PyGuitarPro: construct Song → Track → Measure → Beat → Note tree
  → BeatStroke direction (down/up) per beat
  → write output.gp5
```

---

## Intermediate JSON Schema

This is the contract between Stage 1 and Stage 3. Stage 2 may modify `voicing` and `beats[].direction`.

```json
{
  "title": "song name",
  "tempo": 120,
  "time_signature": "4/4",
  "key": "Am",
  "measures": [
    {
      "measure_number": 1,
      "chord_name": "Am",
      "chord_root": "A",
      "chord_quality": "minor",
      "duration_beats": 4,
      "avg_velocity": 88,
      "voicing": [null, 0, 2, 2, 1, 0],
      "beats": [
        {
          "beat_position": 1.0,
          "direction": "down",
          "duration": "quarter",
          "duration_gp_value": 4,
          "velocity": 95,
          "onset_order": "ascending"
        },
        {
          "beat_position": 1.5,
          "direction": "up",
          "duration": "eighth",
          "duration_gp_value": 8,
          "velocity": 60,
          "onset_order": "descending"
        }
      ]
    }
  ]
}
```

**Field notes**:
- `voicing`: array of 6 integers, index 0 = string 1 (high E), index 5 = string 6 (low E), `null` = muted/not played
- `duration_gp_value`: Guitar Pro duration code — `1`=whole, `2`=half, `4`=quarter, `8`=eighth, `16`=sixteenth
- `onset_order`: `"ascending"` (low pitch first → down strum), `"descending"` (high pitch first → up strum), `"simultaneous"` (use beat-position heuristic)

---

## Stage 1 Implementation: `extract_chords.py`

```python
"""
Stage 1: Parse MIDI → extract chords + strum hints → song_data.json
Usage: python extract_chords.py input.mid
"""

import sys
import json
from music21 import converter, tempo, meter
import pretty_midi

DURATION_MAP = {
    4.0: (4, "whole"), 2.0: (2, "half"), 1.0: (4, "quarter"),
    0.5: (8, "eighth"), 0.25: (16, "sixteenth")
}

def quantize_duration(ql):
    """Snap a music21 quarterLength to nearest standard duration."""
    candidates = list(DURATION_MAP.keys())
    closest = min(candidates, key=lambda x: abs(x - ql))
    return DURATION_MAP[closest]

def detect_onset_order(pm_notes, start_time, window_ms=40):
    """
    Within a chord cluster (notes starting within window_ms of each other),
    check if pitches arrive low-to-high (ascending → down strum) or
    high-to-low (descending → up strum).
    """
    window = window_ms / 1000.0
    cluster = [n for n in pm_notes if abs(n.start - start_time) < window]
    if len(cluster) < 2:
        return "simultaneous"
    sorted_by_onset = sorted(cluster, key=lambda n: n.start)
    pitches_in_order = [n.pitch for n in sorted_by_onset]
    if pitches_in_order == sorted(pitches_in_order):
        return "ascending"   # low pitch first → down strum
    elif pitches_in_order == sorted(pitches_in_order, reverse=True):
        return "descending"  # high pitch first → up strum
    return "simultaneous"

def infer_direction(beat_position, onset_order, time_sig_numerator):
    """
    Heuristic strum direction.
    Priority: onset_order > beat position rule.
    """
    if onset_order == "ascending":
        return "down"
    if onset_order == "descending":
        return "up"
    # Beat-position fallback: on-beat = down, off-beat = up
    frac = beat_position % 1.0
    return "down" if frac < 0.05 else "up"

def extract(midi_path):
    score = converter.parse(midi_path)
    chords_stream = score.chordify()

    # Extract global metadata
    ts_obj = score.recurse().getElementsByClass(meter.TimeSignature).first()
    bpm_obj = score.recurse().getElementsByClass(tempo.MetronomeMark).first()
    ts_str = f"{ts_obj.numerator}/{ts_obj.denominator}" if ts_obj else "4/4"
    bpm = int(bpm_obj.number) if bpm_obj else 120
    ts_num = int(ts_str.split('/')[0])

    # Load pretty_midi for velocity/onset data
    pm = pretty_midi.PrettyMIDI(midi_path)
    all_pm_notes = [n for inst in pm.instruments for n in inst.notes]

    key_obj = score.analyze('key')
    key_str = f"{key_obj.tonic.name} {key_obj.mode}"

    measures = []
    for chord_obj in chords_stream.recurse().getElementsByClass('Chord'):
        offset = float(chord_obj.offset)
        ql = float(chord_obj.duration.quarterLength)
        gp_val, dur_name = quantize_duration(ql)

        chord_name = chord_obj.pitchedCommonName
        root = chord_obj.root().name if chord_obj.root() else "C"
        quality = chord_obj.quality  # 'major', 'minor', 'dominant-seventh', etc.
        avg_vel = int(sum(n.volume.velocity or 80 for n in chord_obj.notes)
                      / len(chord_obj.notes))

        # Onset order for strum direction
        # Convert music21 offset (quarter lengths) to seconds via tempo
        onset_sec = pm.tick_to_time(
            int(offset * pm.resolution)
        ) if pm.resolution else offset * (60.0 / bpm)
        onset_order = detect_onset_order(all_pm_notes, onset_sec)
        direction = infer_direction(offset, onset_order, ts_num)

        # Approximate voicing (null = muted) — mingus will refine in Stage 3
        voicing = [None, None, None, None, None, None]

        beat_entry = {
            "beat_position": round(offset % ts_num, 3),
            "direction": direction,
            "duration": dur_name,
            "duration_gp_value": gp_val,
            "velocity": avg_vel,
            "onset_order": onset_order
        }

        # Group multiple beats per measure if same chord continues
        if (measures and
                measures[-1]["chord_name"] == chord_name and
                measures[-1]["measure_number"] == int(offset // ts_num) + 1):
            measures[-1]["beats"].append(beat_entry)
        else:
            measures.append({
                "measure_number": int(offset // ts_num) + 1,
                "chord_name": chord_name,
                "chord_root": root,
                "chord_quality": quality,
                "duration_beats": round(ql, 2),
                "avg_velocity": avg_vel,
                "voicing": voicing,
                "beats": [beat_entry]
            })

    result = {
        "title": midi_path.rsplit('/', 1)[-1].replace('.mid', ''),
        "tempo": bpm,
        "time_signature": ts_str,
        "key": key_str,
        "measures": measures
    }

    out_path = midi_path.replace('.mid', '_song_data.json')
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {len(measures)} measures to {out_path}")
    return out_path

if __name__ == '__main__':
    extract(sys.argv[1])
```

---

## Stage 3 Implementation: `build_gp5.py`

```python
"""
Stage 3: song_data_enhanced.json → output.gp5
Usage: python build_gp5.py song_data_enhanced.json
"""

import sys
import json
import guitarpro as gp
from mingus.extra.tunings import get_tuning
from mingus.containers import NoteContainer

# Standard EADGBE tuning: string number → MIDI pitch
STANDARD_TUNING_MIDI = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40}

def get_best_voicing(chord_name, existing_voicing=None):
    """
    Use mingus to find guitar fingerings for a chord.
    If existing_voicing has non-null values, use it directly.
    Returns list of 6 ints (0-based string index), None = muted.
    """
    if existing_voicing and any(v is not None for v in existing_voicing):
        return existing_voicing

    try:
        guitar = get_tuning('guitar', 'standard', 6, 1)
        nc = NoteContainer().from_chord(chord_name)
        fingerings = guitar.find_chord_fingering(
            nc, max_distance=4, maxfret=12, max_fingers=4
        )
        if fingerings:
            # Prefer fingerings with more open strings (nulls/0s = easier to play)
            def open_string_count(f):
                return sum(1 for x in f if x == 0 or x is None)
            return sorted(fingerings, key=open_string_count, reverse=True)[0]
    except Exception as e:
        print(f"  Warning: mingus couldn't find voicing for {chord_name}: {e}")

    # Hard-coded fallback for common chords
    fallbacks = {
        'A major': [None, 0, 2, 2, 2, 0],
        'A minor': [None, 0, 2, 2, 1, 0],
        'C major': [None, 3, 2, 0, 1, 0],
        'D major': [None, None, 0, 2, 3, 2],
        'E major': [0, 2, 2, 1, 0, 0],
        'E minor': [0, 2, 2, 0, 0, 0],
        'F major': [1, 1, 2, 3, 3, 1],
        'G major': [3, 2, 0, 0, 0, 3],
    }
    return fallbacks.get(chord_name, [0, 0, 0, 0, 0, 0])

def build_gp5(json_path):
    with open(json_path) as f:
        data = json.load(f)

    song = gp.Song()
    song.title = data.get('title', 'Untitled')
    song.tempo = data.get('tempo', 120)

    # Configure acoustic guitar track
    track = song.tracks[0]
    track.name = "Acoustic Guitar"
    track.isPercussionTrack = False
    track.strings = [
        gp.GuitarString(number=i, value=STANDARD_TUNING_MIDI[i])
        for i in range(1, 7)
    ]

    ts_str = data.get('time_signature', '4/4')
    ts_num, ts_den = map(int, ts_str.split('/'))

    # Build measures
    num_measures = len(data['measures'])
    while len(song.measureHeaders) < num_measures:
        header = gp.MeasureHeader()
        header.number = len(song.measureHeaders) + 1
        header.timeSignature.numerator = ts_num
        header.timeSignature.denominator.value = ts_den
        song.measureHeaders.append(header)
        for t in song.tracks:
            t.measures.append(gp.Measure(t, header))

    for i, mdata in enumerate(data['measures']):
        if i >= len(track.measures):
            break

        measure = track.measures[i]
        voice = measure.voices[0]
        voice.beats.clear()

        chord_name = mdata.get('chord_name', 'C major')
        voicing = get_best_voicing(chord_name, mdata.get('voicing'))
        print(f"  Measure {i+1}: {chord_name} → voicing {voicing}")

        for beat_data in mdata.get('beats', []):
            beat = gp.Beat(voice=voice)

            # Duration
            gp_val = beat_data.get('duration_gp_value', 4)
            beat.duration = gp.Duration(value=gp_val)

            # Strum direction
            direction_str = beat_data.get('direction', 'down')
            stroke_dir = (gp.BeatStrokeDirection.down
                          if direction_str == 'down'
                          else gp.BeatStrokeDirection.up)
            beat.effect.stroke = gp.BeatStroke(direction=stroke_dir, value=8)

            # Notes from voicing
            # voicing[0] = string 1 (high E), voicing[5] = string 6 (low E)
            vel = beat_data.get('velocity', 85)
            for str_idx, fret in enumerate(voicing):
                if fret is None:
                    continue
                string_num = str_idx + 1  # 1-indexed
                note = gp.Note(
                    beat=beat,
                    value=fret,
                    string=string_num,
                    velocity=min(127, max(1, vel)),
                    type=gp.NoteType.normal
                )
                beat.notes.append(note)

            voice.beats.append(beat)

    out_path = json_path.replace('_enhanced.json', '.gp5').replace('.json', '.gp5')
    gp.write(song, out_path)
    print(f"\nWrote {out_path}")

if __name__ == '__main__':
    build_gp5(sys.argv[1])

```

---

## Stage 2: What Claude Should Do With the JSON

When reviewing `song_data.json`, apply these musical judgment rules before writing `song_data_enhanced.json`:

**Voicing selection rules (update `voicing` field):**
- Prefer **open chord voicings** (contain 0-fret open strings) over barre chords where musically equivalent
- If 3+ consecutive chords require barre positions, suggest a **capo** (add `"capo": N` to the top-level JSON) and re-express voicings relative to the capo
- For chords like `C#m`, `F#`, `Bb`: strongly consider capo instead of barre
- Prefer voicings where adjacent chords share common fingers (smooth voice leading)

**Strumming pattern rules (update `beats[].direction`):**
- In 4/4: default pattern is `D D↑ ↑D↑` per bar unless velocity data clearly says otherwise
- In 3/4: `D D↑ D↑` (waltz) or `D D D` (simple)
- In 6/8: `D ↑ D ↑ D ↑` with accents on beats 1 and 4
- Downstrokes on **structural beats** (beat 1 always, beat 3 in 4/4)
- High-velocity notes (> 100) → force `"down"`; low-velocity (< 50) → force `"up"`

**Extensions/embellishments (may update `chord_name`):**
- `G major` that resolves to `C major` → consider `Gsus4` or `G/B`
- `A minor` with velocity buildup → consider `Am7` or `Asus2`
- Only add extensions if the original MIDI notes actually support them (check `chord_name` field for note content)

**Flag problems** by adding `"warning": "..."` to any measure where:
- The chord has no clean guitar voicing within 5 frets
- The transition from the previous chord is a stretch of > 7 frets
- The chord name is ambiguous or unrecognized

---

## Running the Full Pipeline

```bash
# Step 1: Extract
python extract_chords.py my_song.mid
# → produces my_song_song_data.json

# Step 2: (Claude Code reviews and enhances the JSON)
# Feed song_data.json to Claude, ask it to apply the Stage 2 rules above
# Save result as song_data_enhanced.json

# Step 3: Build GP5
python build_gp5.py song_data_enhanced.json
# → produces song_data_enhanced.gp5  (or output.gp5)

# Open in Guitar Pro / TuxGuitar for manual polish
```

---

## Known Limitations & Edge Cases

| Issue | Cause | Mitigation |
|---|---|---|
| Chord detection wrong on dense keyboard parts | music21 chordify merges all voices | Try isolating the piano's left hand (chord) track before parsing if MIDI is multi-track |
| Timing feels stiff | Quantized MIDI has no humanization | Accept it — Guitar Pro's Human Playing option can add feel back |
| Mingus can't find voicing | Exotic chord (e.g. Lydian #9) | Fallback dict in `build_gp5.py` + Claude override in Stage 2 |
| PyGuitarPro crashes on write | Measure has zero beats | Ensure every measure has at least one beat; add a rest beat if needed |
| GP5 opens with wrong tempo | music21 tempo extraction missed header | Manually verify BPM in `song_data.json` before Stage 3 |
| GP7/GP8 native format needed | PyGuitarPro only writes GP5 | GP5 opens fine in GP7/8; or use the alphaTex → Node.js → `.gp` path (see below) |

---

## Optional: alphaTex Path for GP7 Native Format

If `.gp5` is not acceptable and GP7 native format is required:

Instead of Stage 3 with PyGuitarPro, generate alphaTex text and convert via Node.js:

```
# alphaTex syntax reference for acoustic guitar strumming:
\title "Song Name"
\tempo 120
\track "Acoustic Guitar"
\staff {tabs}
\tuning E4 B3 G3 D3 A2 E2
\ts 4 4

# Note syntax: fret.string  (string 1 = high E)
# Chord: (fret1.str1 fret2.str2 ...) all notes in parens
# Strum down: { sd }   Strum up: { su }
# Duration: :4 = quarter, :8 = eighth (sticky until changed)

:4 (0.1 1.2 2.3 2.4 0.5) { sd }
:8 (0.1 1.2 2.3 2.4 0.5) { su } (0.1 1.2 2.3 2.4 0.5) { su }
:4 (0.1 1.2 2.3 2.4 0.5) { sd } |
```

Convert to GP file:
```bash
npm install @coderline/alphatab
node convert_alphatex.js song.alphatex output.gp
```

```javascript
// convert_alphatex.js
const alphaTab = require('@coderline/alphatab');
const fs = require('fs');
const importer = new alphaTab.importer.AlphaTexImporter();
importer.initFromString(fs.readFileSync(process.argv[2], 'utf8'));
const score = importer.readScore();
const exporter = new alphaTab.exporter.Gp7Exporter();
const data = exporter.export(score, new alphaTab.Settings());
fs.writeFileSync(process.argv[3], Buffer.from(data));
```

**LLM advantage**: Claude can generate alphaTex text far more reliably than constructing PyGuitarPro object graphs. If asking Claude to write the tab directly, use alphaTex, not Python object trees.

---

## Quick Reference: Guitar Pro 5 Data Model (for PyGuitarPro)

```
Song
  .title, .tempo, .measureHeaders[]
  .tracks[]
    Track
      .name, .strings[], .isPercussionTrack
      .measures[]
        Measure (linked to MeasureHeader for time sig)
          .voices[]  (usually 2; use voices[0])
            Voice
              .beats[]
                Beat
                  .duration (Duration with .value = 1/2/4/8/16)
                  .notes[]
                    Note
                      .string (1=high E, 6=low E)
                      .value  (fret number, 0=open)
                      .velocity (1-127)
                      .type (NoteType.normal / .tie / .dead)
                  .effect
                    BeatEffect
                      .stroke → BeatStroke(direction, value)
                        direction: BeatStrokeDirection.down / .up
                        value: speed (8 = eighth-note brush speed)
```
