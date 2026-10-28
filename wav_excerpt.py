"""Frame-exact PCM16 excerpt packets. Standard library only; see README.md."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile

VERSION = "1.0.0"
MAX_SOURCE = 64 * 1024 * 1024
MAX_RECIPE = 1024 * 1024
MAX_CHUNKS = 256
MAX_RANGES = 1000
MAX_OUTPUT = 256 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024


class Rejected(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Rejected(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_bounded(path, limit, kind):
    with open(path, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode), kind + " must be a regular file.")
        require(info.st_size <= limit, kind + " exceeds " + str(limit) + " bytes.")
        raw = handle.read(limit + 1)
        require(len(raw) <= limit, kind + " grew beyond its byte limit.")
        require(len(raw) == info.st_size, kind + " changed size during the read; retry from a stable copy.")
        return raw


def parse_wave(raw):
    require(len(raw) >= 12, "Malformed WAV: missing RIFF header.")
    require(raw[:4] == b"RIFF", "Unsupported container: only little-endian RIFF is supported (not RF64/RIFX).")
    require(raw[8:12] == b"WAVE", "Unsupported RIFF form: expected WAVE.")
    require(struct.unpack_from("<I", raw, 4)[0] + 8 == len(raw), "Malformed WAV: RIFF extent differs from file length.")
    pos, fmt, data, chunks = 12, None, None, []
    while pos < len(raw):
        require(len(chunks) < MAX_CHUNKS, "WAV exceeds 256 chunks.")
        require(pos + 8 <= len(raw), "Malformed WAV: incomplete chunk header.")
        kind = raw[pos:pos + 4]
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        start, end = pos + 8, pos + 8 + size
        require(end + (size % 2) <= len(raw), "Malformed WAV: chunk payload or odd padding exceeds RIFF extent.")
        chunks.append({"id_hex": kind.hex(), "payload_bytes": size})
        if kind == b"fmt ":
            require(fmt is None, "Malformed WAV: duplicate fmt chunk.")
            require(size in (16, 18), "Unsupported fmt size: require 16, or 18 with cbSize zero.")
            if size == 18:
                require(struct.unpack_from("<H", raw, start + 16)[0] == 0, "Unsupported fmt extension: cbSize must be zero.")
            tag, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", raw, start)
            require(tag == 1, "Unsupported encoding: require format tag 1, not float/compressed/extensible.")
            require(bits == 16 and channels in (1, 2), "Unsupported format: require signed PCM16 mono or stereo.")
            require(8000 <= rate <= 96000, "Unsupported sample rate: require integer 8000 through 96000 Hz.")
            require(align == channels * 2, "Malformed WAV: inconsistent block alignment.")
            require(byte_rate == rate * align, "Malformed WAV: inconsistent byte rate.")
            fmt = {"channels": channels, "sample_rate_hz": rate, "bits_per_sample": bits,
                   "block_align_bytes": align, "format_tag": tag, "original_fmt_bytes": size}
        elif kind == b"data":
            require(data is None, "Malformed WAV: duplicate data chunk.")
            data = (start, size)
        pos = end + (size % 2)
    require(fmt is not None and data is not None, "Malformed WAV: one fmt and one data chunk are required.")
    require(data[1] > 0 and data[1] % fmt["block_align_bytes"] == 0, "Malformed WAV: empty data or partial PCM frame.")
    return {"format": fmt, "data_offset": data[0], "data_bytes": data[1],
            "frames": data[1] // fmt["block_align_bytes"], "chunks": chunks,
            "omitted_chunks": [c for c in chunks if c["id_hex"] not in ("666d7420", "64617461")]}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Recipe has a duplicate JSON key.")
        result[key] = value
    return result


def parse_recipe(raw, frames):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Rejected("Recipe must be UTF-8 JSON.") from error
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            require(depth <= 4, "Recipe nesting exceeds four containers.")
        elif char in "]}":
            depth -= 1
    def constant(_):
        raise Rejected("Recipe cannot contain NaN or Infinity.")
    def integer(token):
        require(len(token) <= 12, "Recipe integer is longer than 12 characters.")
        return int(token)
    def fractional(_):
        raise Rejected("Recipe numbers must be integers, not decimal/exponent notation.")
    try:
        obj = json.loads(text, object_pairs_hook=unique_object, parse_constant=constant,
                         parse_int=integer, parse_float=fractional)
    except (ValueError, RecursionError) as error:
        raise Rejected("Recipe is not valid JSON.") from error
    require(type(obj) is dict and set(obj) == {"version", "allow_overlap", "ranges"},
            "Recipe requires exactly version, allow_overlap and ranges.")
    require(type(obj["version"]) is int and obj["version"] == 1, "Recipe version must be integer 1.")
    require(type(obj["allow_overlap"]) is bool, "allow_overlap must be true or false.")
    require(type(obj["ranges"]) is list and 1 <= len(obj["ranges"]) <= MAX_RANGES, "Recipe requires 1 through 1000 ranges.")
    ranges = []
    for i, item in enumerate(obj["ranges"], 1):
        require(type(item) is dict and {"start", "end"} <= set(item) <= {"start", "end", "label"},
                "Range " + str(i) + " requires start/end and optional label only.")
        start, end = item["start"], item["end"]
        require(type(start) is int and type(end) is int, "Range " + str(i) + " frame values must be integers, not booleans or decimals.")
        require(0 <= start < end <= frames, "Range " + str(i) + " must satisfy 0 <= start < end <= " + str(frames) + ". Nothing is clamped.")
        label = item.get("label", "")
        require(type(label) is str and len(label) <= 120, "Range label must be a string of at most 120 Unicode code points.")
        ranges.append({"start": start, "end": end, "label": label})
    if not obj["allow_overlap"]:
        ordered = sorted(ranges, key=lambda x: x["start"])
        require(all(a["end"] <= b["start"] for a, b in zip(ordered, ordered[1:])),
                "Ranges overlap; explicitly set allow_overlap true if intended.")
    return {"version": 1, "allow_overlap": obj["allow_overlap"], "ranges": ranges}


def header(channels, rate, frames):
    size = frames * channels * 2
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + size, b"WAVE", b"fmt ", 16,
                       1, channels, rate, rate * channels * 2, channels * 2, 16, b"data", size)


def encode_manifest(obj):
    raw = (json.dumps(obj, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    require(len(raw) <= MAX_MANIFEST, "Manifest exceeds 4 MiB; no output produced.")
    return raw


def prepare(raw, recipe_raw):
    info = parse_wave(raw)
    recipe = parse_recipe(recipe_raw, info["frames"])
    fmt, align = info["format"], info["format"]["block_align_bytes"]
    total = sum(44 + (r["end"] - r["start"]) * align for r in recipe["ranges"])
    require(total <= MAX_OUTPUT, "Planned WAV output exceeds 256 MiB; overlapping ranges count fully.")
    source_data = memoryview(raw)[info["data_offset"]:info["data_offset"] + info["data_bytes"]]
    outputs = []
    for i, r in enumerate(recipe["ranges"], 1):
        count = r["end"] - r["start"]
        start = info["data_offset"] + r["start"] * align
        end = info["data_offset"] + r["end"] * align
        payload = memoryview(raw)[start:end]
        digest = hashlib.sha256(header(fmt["channels"], fmt["sample_rate_hz"], count))
        digest.update(payload)
        rate = fmt["sample_rate_hz"]
        outputs.append({"ordinal": i, "filename": "excerpt-" + str(i).zfill(4) + ".wav",
                        "label": r["label"], "source_start_frame": r["start"], "source_end_frame_exclusive": r["end"],
                        "source_byte_start": start, "source_byte_end_exclusive": end, "frames": count,
                        "file_bytes": 44 + len(payload), "file_sha256": digest.hexdigest(), "pcm_sha256": sha(payload),
                        "time_seconds_exact": {"start_numerator": r["start"], "end_numerator": r["end"], "denominator": rate},
                        "time_seconds_display_rounded": {"start": format(r["start"] / rate, ".9f"), "end": format(r["end"] / rate, ".9f")}})
    manifest = {"schema_version": 1, "tool": "wav-excerpt-kit", "tool_version": VERSION, "state": "plan",
                "source": {"file_bytes": len(raw), "file_sha256": sha(raw), "pcm_sha256": sha(source_data),
                           "frames": info["frames"], "data_offset": info["data_offset"], "data_bytes": info["data_bytes"], "format": fmt},
                "recipe": recipe, "total_wav_bytes": total, "outputs": outputs,
                "omitted_source_chunks": info["omitted_chunks"],
                "rules": {"range": "Zero-based [start,end); end exclusive. Output frame j maps to source start+j.",
                          "container": "Fresh minimal fmt/data WAV; PCM frame bytes preserved, other source metadata omitted.",
                          "time": "Exact fractions use sample-clock frames/rate, not wall-clock recording time. Decimal displays are rounded.",
                          "limits": "No audio/context/meaning/quality assessment. Cuts may click or remove context. Local files may be sensitive."}}
    encode_manifest(manifest)
    return manifest


def export_packet(raw, plan, out_name):
    require(sys.platform == "win32", "Export is supported on Windows only in this release; no-replacement finalization is not claimed on other platforms.")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {"COM" + str(i) for i in range(1, 10)} | {"LPT" + str(i) for i in range(1, 10)}
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", out_name) is not None and out_name.upper() not in reserved,
            "Output must be a new simple folder name (1-64 ASCII letters/digits/_/-; no path, dots or reserved device name).")
    parent = Path.cwd().resolve(strict=True)
    destination = parent / out_name
    require(not os.path.lexists(destination), "Destination already exists; it will not be replaced.")
    manifest = dict(plan, state="complete")
    encoded = encode_manifest(manifest)
    stage = Path(tempfile.mkdtemp(prefix=".wav-excerpt-staging-", dir=parent))
    try:
        fmt = plan["source"]["format"]
        for item in plan["outputs"]:
            payload = memoryview(raw)[item["source_byte_start"]:item["source_byte_end_exclusive"]]
            target = stage / item["filename"]
            with open(target, "xb") as handle:
                handle.write(header(fmt["channels"], fmt["sample_rate_hz"], item["frames"]))
                handle.write(payload)
            actual = read_bounded(target, MAX_SOURCE, "Generated WAV")
            require(len(actual) == item["file_bytes"] and sha(actual) == item["file_sha256"], "Generated WAV failed byte/hash verification.")
        with open(stage / "manifest.json", "xb") as handle:
            handle.write(encoded)
        require((stage / "manifest.json").read_bytes() == encoded, "Manifest write verification failed.")
        # Windows os.rename refuses an existing destination, including a late arrival.
        # Staging and destination share the resolved current directory / volume.
        os.rename(stage, destination)
    except BaseException as error:
        # No recursive cleanup: never follow or delete a concurrently introduced link.
        raise Rejected("Export did not complete. Any pre-existing destination is untouched. Private staging may remain at "
                       + ascii(str(stage)) + "; inspect it before manual removal. Cause: " + ascii(str(error))) from error
    return {"state": "complete", "directory": out_name, "excerpts": len(plan["outputs"]), "total_wav_bytes": plan["total_wav_bytes"]}


def fixture_bytes():
    # 12 synthetic stereo frames; odd opaque JUNK with nonzero pad catches offset assumptions.
    pcm = b"".join(struct.pack("<hh", n - 6, 100 * n) for n in range(12))
    fmt = struct.pack("<HHIIHH", 1, 2, 8000, 32000, 4, 16)
    body = b"WAVE" + b"JUNK" + struct.pack("<I", 3) + b"abc" + b"\x7f" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body


def main():
    parser = argparse.ArgumentParser(description="Prepare frame-exact PCM16 excerpt packets. No network, playback or overwrite.")
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fixture", help="Write a synthetic non-speech WAV to a new file.")
    f.add_argument("file")
    sub.add_parser("info", help="Read source frame count and format before choosing ranges.").add_argument("source")
    for command in ("plan", "export"):
        p = sub.add_parser(command)
        p.add_argument("source")
        p.add_argument("--recipe", required=True)
        if command == "export":
            p.add_argument("--out", required=True, help="New simple folder name in the current directory; Windows only.")
    args = parser.parse_args()
    try:
        if args.command == "fixture":
            raw = fixture_bytes()
            with open(args.file, "xb") as handle:
                handle.write(raw)
            result = {"state": "synthetic_fixture_created", "bytes": len(raw), "sha256": sha(raw), "frames": 12, "sample_rate_hz": 8000}
        else:
            raw = read_bounded(args.source, MAX_SOURCE, "Source")
            if args.command == "info":
                result = {"state": "source_facts", "file_bytes": len(raw), "file_sha256": sha(raw), **parse_wave(raw)}
            else:
                recipe = read_bounded(args.recipe, MAX_RECIPE, "Recipe")
                plan = prepare(raw, recipe)
                result = plan if args.command == "plan" else export_packet(raw, plan, args.out)
        print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
        return 0
    except (Rejected, OSError, ValueError, OverflowError) as error:
        print("REJECTED: " + ascii(str(error)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
