# Reproducible WAV excerpt packets

Prepare several precise audio excerpts for a transcript reviewer, with a reusable recipe and an exact route back to the original frames. This is a local, dependency-free Python command-line kit. It copies selected PCM sample bytes into new WAV containers; it does not listen, transcribe, translate or choose meaningful boundaries.

**Export requires Windows and an installed Python 3.10 or later.** Development checks use Python 3.10.2 on Windows; other interpreter/platform combinations are not established by that check. The no-replacement directory finalization deliberately refuses non-Windows platforms. There are no packages to install. Run `python --version` first. Use a private local folder, not a shared or concurrently modified directory.

## Start from the downloaded bundle

Extract `wav-excerpt-kit.zip` into a new private folder. Open a terminal in that folder. The bundle contains this README, the MIT license, two Python scripts and two small example recipes. No audio recording is bundled. Run these commands one at a time:

```text
python wav_excerpt.py fixture demo.wav
python wav_excerpt.py info demo.wav
python wav_excerpt.py plan demo.wav --recipe example-ranges.json
python wav_excerpt.py export demo.wav --recipe example-ranges.json --out review-packet
python verify_packet.py demo.wav review-packet
```

The fixture is **synthetic, not speech**: 12 stereo frames at 8,000 Hz. `info` reports 104 source bytes, 12 frames and a data offset of 56, not 44. `plan` lists every requested range, ordinal filename, predicted hash and the total WAV output size. Its state is `plan`; no excerpt has been written. Review that complete plan before exporting.

The two example windows are `[2,5)` and `[8,12)`. Export creates `review-packet/excerpt-0001.wav` (56 bytes), `excerpt-0002.wav` (60 bytes) and `manifest.json`. The WAV total is 116 bytes; the manifest is additional. The verifier reports two exact source-slice matches. Its partition result is `null` because these selected windows do not cover the whole source. `null` is not failure.

To verify a complete partition, run:

```text
python wav_excerpt.py export demo.wav --recipe example-partition.json --out full-partition
python verify_packet.py demo.wav full-partition
```

That recipe selects `[0,5)`, `[5,10)` and `[10,12)`. The final range is explicitly shorter. The verifier concatenates **PCM payloads**, not whole WAV files, and confirms that all 48 original PCM bytes are reconstructed. Repeating the same commands with an existing fixture filename or output folder refuses replacement. Choose a genuinely new folder name for another run; there is no force flag.

## Prepare your own selection

Keep the original recording and use `info` to read its frame count, rate and supported format. Make a copy of an example recipe, then choose ranges from the material you actually intend to review. This kit does not play audio; use your normal player/editor to establish the needed context. Frame indexes are precise sample locations, not words or sentences.

The complete recipe schema is:

```json
{
  "version": 1,
  "allow_overlap": false,
  "ranges": [
    {"start": 2, "end": 5, "label": "A private review note"}
  ]
}
```

`start` and `end` are **zero-based integer frame indexes**, with an exclusive end: `0 <= start < end <= source_frames`. A stereo frame contains one sample for each channel. `[2,5)` includes frames 2, 3 and 4. At 8,000 Hz, its start is exactly `2/8000` seconds and its duration is `3/8000` seconds. Decimal displays in the manifest are rounded; the integer numerator and sample-rate denominator are authoritative. These are offsets on the sample clock, not wall-clock recording times.

The top-level keys must be exactly `version`, `allow_overlap` and `ranges`. Each range requires `start` and `end`; `label` is optional and defaults to an empty string. Labels allow up to 120 Unicode code points and never become filenames. Range order is retained even if it differs from time order. Set `allow_overlap` to `true` only when repeated source frames are intended; overlap otherwise rejects the whole recipe. Overlap is not a complete partition.

Unknown or duplicate keys, booleans as frame numbers, decimal/exponent numbers, NaN/Infinity, integer tokens longer than 12 characters, deeper than four JSON containers, empty ranges and out-of-bounds values reject. JSON must be UTF-8 without a BOM. There is no decimal-time input, automatic chunk-size mode, clamping, dropping or guessing of frames. List every intended window explicitly. A single invalid late window prevents all final output.

## What is preserved, and what changes

For each window, output PCM equals the source data slice from `start * block_align` through `end * block_align`. Channel order/interleaving, signed integer samples, sample rate and bit depth remain unchanged. There is no gain, downmix, fade, resampling or normalization. Abrupt cuts can click or remove context; exact bytes do not establish useful or intelligible speech.

Each result has a fresh minimal 44-byte `fmt `/`data` header followed by the selected frames. **Exact PCM does not mean identical file bytes or preserved metadata.** Other source chunks are omitted, including potentially stale cue, loop and recording-time metadata. Their identifiers are listed as hexadecimal bytes with payload lengths; their payloads are never copied into the manifest. Removing metadata is not anonymization.

The supported subset is little-endian RIFF/WAVE, tag 1 signed PCM16, one or two channels, and integer sample rates 8,000–96,000 Hz inclusive. There must be one nonempty `data` and one `fmt ` chunk, in either order. `fmt ` is 16 bytes, or 18 bytes with `cbSize=0`. RIFF extent must equal the actual file length; all chunk headers, payloads and odd padding must fit. Padding values are ignored. Byte rate and block alignment must agree with the format, and data must contain complete frames. Unknown chunks receive outer-extent checks only.

RF64, RIFX, float, compressed, extensible, other widths/channels/rates and other fmt sizes are unsupported by this tool, not necessarily invalid files. Duplicate required chunks, truncated extents, inconsistent rates/alignment and empty/partial data are malformed for this declared input. A filename or MIME type proves neither support nor validity. Python's `wave` reader may accept more formats than this kit.

## Bounds and failure behavior

| Resource | Inclusive limit |
| --- | --- |
| Source file | 64 MiB (67,108,864 bytes), size checked before large read |
| Source chunks | 256 |
| Recipe | 1 MiB (1,048,576 bytes) |
| Excerpts | 1–1,000 |
| Planned WAV output | 256 MiB (268,435,456 bytes), including each 44-byte header |
| Manifest | 4 MiB (4,194,304 bytes) |

Overlapping windows count fully toward output volume. Bounds reject rather than truncate. The complete manifest is encoded and checked before any staging output. Work holds the bounded source snapshot in memory; keep source/recipe files stable during the command. The hashes identify the bytes actually read, not a locked or authenticated recording.

`--out` accepts a **new simple folder name in the current directory**, 1–64 ASCII letters/digits/underscores/hyphens, beginning with a letter or digit. Paths, dots, separators and Windows reserved device names are rejected. Files use fixed ordinal names. The source and recipe are read-only; the exporter never deletes or replaces an existing destination.

After validation it creates a unique `.wav-excerpt-staging-...` sibling folder. It writes and checks all WAVs and the manifest, then uses Windows `os.rename`, which refuses an existing destination even if it appeared after the initial check. Successful finalization returns `state: complete`. An I/O failure returns nonzero, does not present a partial final packet as complete, and reports any remaining private staging path. **No automatic recursive cleanup runs.** Inspect that exact staging folder before any manual removal. Unexpected process termination can also leave staging. This is not a power-loss/durability guarantee or protection against an adversary modifying your working directory.

## Read and verify the handoff

`manifest.json` records the tool/schema versions; source-file and source-PCM SHA-256; source byte/frame counts, format and data offset; canonical recipe; every output filename, source frame range and absolute source byte span; exact rational times; file and PCM hashes; and omitted metadata descriptors. It omits source names, absolute source paths and recording wall-clock guesses. It contains labels you supplied, which may still be sensitive.

For output frame `j`, the original frame is `source_start_frame + j`. The separate `verify_packet.py` checks raw source locations and opens the output WAVs with Python's `wave` reader. It compares actual samples and complete hashes, rejects extra/missing packet files and verifies full reconstruction only for an ordered contiguous partition. It is a consistency check, not proof of author identity, trusted provenance, audibility or review completion. It reads locally; it does not write or repair files.

There are no volatile timestamps in the packet, so the same source snapshot, canonical recipe and tool version produce the same files. A differently formatted equivalent recipe has the same canonical representation. Retain the original privately alongside your packet; a source hash alone cannot recover missing audio.

## Reproduce the synthetic numbers

For `n=0..11`, the fixture's left sample is `n-6` and its right sample is `100*n`, stored as interleaved little-endian signed 16-bit integers. It has an odd three-byte `JUNK` payload plus a nonzero padding byte before `fmt `, exposing parsers that assume a 44-byte input header.

The first example excerpt contains left `[-4,-3,-2]` and right `[200,300,400]`. Its 3 frames use 12 PCM bytes and 56 total bytes. The second contains left `[2,3,4,5]` and right `[800,900,1000,1100]`, using 16 PCM bytes and 60 total bytes. These are mathematical fixtures, not field recordings, actual ASR output or listening evidence.

## Privacy, sources and ownership

Processing is offline: no network, telemetry, device access, recording or playback. Deliberate file outputs remain on your machine. Real excerpts contain the selected speech; share only with intended reviewers and do not publish private recordings or labels in repositories. The public tutorial's tiny fixture contains no speech.

The [PewTranslate transcript-review guide](https://pewtranslate.com/en/guides/check-sermon-transcript-translation-errors) describes the human follow-up: return to the recording and resolve uncertain wording with the right reviewer. Byte checks cannot establish meaning or translation accuracy. This is original AI-assisted code and documentation by [PewTranslate](https://pewtranslate.com/), copyright 2026 szj2ys. **Live-translation accounts are closed.** The kit requires no account and claims no available production translation service.

Primary references: [Microsoft RIFF](https://learn.microsoft.com/en-us/windows/win32/xaudio2/resource-interchange-file-format--riff-), [WAVEFORMATEX](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/ns-mmeapi-waveformatex), [Python 3.12 wave](https://docs.python.org/3.12/library/wave.html) and [Python os.rename](https://docs.python.org/3.12/library/os.html#os.rename). They inform the declared subset and operations, not a standards certification or endorsement.

Public files are the tutorial `index.html`, this README, `LICENSE`, `wav_excerpt.py`, `verify_packet.py`, two example JSON recipes and the ZIP containing those six non-HTML files. Local `.verification/` artifacts are excluded. MIT licensed; see `LICENSE`.
