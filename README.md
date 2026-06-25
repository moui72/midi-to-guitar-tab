# midi-to-guitar-tab

Convert DAW-exported MIDI files into playable Guitar Pro 5 (`.gp5`) tabs. Preserves arpeggios, fingerpicking patterns, and original rhythm from the MIDI source -- no chord-smashing into block strums.

## The Problem

When a keyboard player exports a MIDI file, the voicings are polyphonic and the note patterns don't map directly to guitar. Most MIDI-to-tab tools collapse everything into block chords, destroying arpeggiated patterns and altering the rhythm. This project takes a different approach: it maps each MIDI note individually to a guitar string/fret position while preserving the original timing.

## Two Approaches

### 1. Direct Note-Level Conversion (recommended)

`midi_to_gp5.py` reads the raw MIDI notes, clusters simultaneous events, assigns each note to a guitar string using brute-force voicing optimization, and writes the result as a GP5 file. Arpeggios stay as arpeggios. Block chords stay as block chords.

```bash
python midi_to_gp5.py input.mid [output.gp5] [bpm] [--bass]
```

Arguments:
- `input.mid` -- source MIDI file
- `output.gp5` -- output Guitar Pro 5 file (default: `output.gp5`)
- `bpm` -- playback BPM for the GP5 file (default: 72)
- `--bass` -- bass guitar mode: 4-string standard GDAE tuning, Electric Bass instrument

### 2. Three-Stage Chord Pipeline

For cases where you want chord-based analysis and Claude-assisted voicing refinement:

```bash
# Stage 1: Extract chords from MIDI
python extract_chords.py input.mid 72

# Stage 2: Enhance with guitar voicings and strumming
python enhance_json.py input_song_data.json

# Stage 3: Build GP5 from enhanced data
python build_gp5.py input_song_data_enhanced.json output.gp5
```

Stage 2 can also be done manually or with an LLM reviewing the intermediate JSON -- see [MIDI_TO_GP_PIPELINE.md](MIDI_TO_GP_PIPELINE.md) for the full spec.

## How String Assignment Works

For each MIDI note (or cluster of simultaneous notes), the converter finds valid string/fret combinations within the selected tuning (max fret 15). For chords, it uses brute-force search over all valid combinations to minimize:

```
score = max_fret * 10 + fret_spread * 5 + avg_fret
```

This heavily penalizes high-fret positions and wide stretches, producing voicings that stay in the first 7-8 frets when possible. All notes get a `let ring` effect for natural sustain.

## What You Get

- Single-track tab in GP5 format
- Guitar mode: Acoustic Guitar, standard EADGBE tuning (6 strings)
- Bass mode: Electric Bass, standard GDAE tuning (4 strings)
- Arpeggios preserved as individual notes on separate beats
- Block chords with optimized fret positions
- Correct time signature and beat durations
- Opens in Guitar Pro 7/8, TuxGuitar, or any GP5-compatible editor

## What You'll Need to Fix Manually

This produces a first-draft tab. You'll likely want to:

- Adjust string assignments in arpeggio sections where two sequential notes land on the same string (the tool assigns each note independently)
- Add strum direction markers on block chord sections
- Simplify dense passages that were idiomatic on keyboard but awkward on guitar
- Tweak voicings in sharp/flat-heavy sections where open chord alternatives exist

## Install

```bash
pip install -r requirements.txt
```

Requires Python 3.10+.

## File Overview

| File | Purpose |
|------|---------|
| `midi_to_gp5.py` | Direct MIDI-to-GP5 converter (note-level, preserves arpeggios) |
| `extract_chords.py` | Stage 1: MIDI parsing via music21, chord extraction to JSON |
| `enhance_json.py` | Stage 2: guitar voicing assignment, chord name cleanup, strumming refinement |
| `build_gp5.py` | Stage 3: JSON-to-GP5 builder using PyGuitarPro |
| `MIDI_TO_GP_PIPELINE.md` | Full pipeline specification and intermediate JSON schema |
| `requirements.txt` | Python dependencies |

## License

MIT
