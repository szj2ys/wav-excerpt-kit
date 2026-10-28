"""Read-only independent packet check using raw chunks and Python wave outputs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import wave


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bounded(path, limit):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    checked(len(data) <= limit, "File exceeds verifier bound")
    return data


def check(source, packet):
    original = bounded(source, 64 * 1024 * 1024)
    report = json.loads(bounded(packet / "manifest.json", 4 * 1024 * 1024))
    checked(report["schema_version"] == 1 and report["state"] == "complete", "Require a complete schema-1 manifest")
    checked(digest(original) == report["source"]["file_sha256"] and len(original) == report["source"]["file_bytes"], "Source file identity mismatch")
    checked(original[:4] == b"RIFF" and original[8:12] == b"WAVE", "Source container mismatch")
    checked(int.from_bytes(original[4:8], "little") + 8 == len(original), "Source RIFF extent mismatch")
    offset, data_start, data_end, format_values, count, fmt_size = 12, None, None, None, 0, None
    omitted = []
    while offset < len(original):
        checked(offset + 8 <= len(original), "Truncated source chunk")
        size = int.from_bytes(original[offset + 4:offset + 8], "little")
        a, z = offset + 8, offset + 8 + size
        checked(z + (size % 2) <= len(original), "Source chunk outside file")
        kind = original[offset:offset + 4]
        if kind == b"data":
            checked(data_start is None, "Duplicate data")
            data_start, data_end = a, z
        if kind == b"fmt ":
            checked(format_values is None and size in (16, 18), "Invalid fmt")
            format_values = struct.unpack_from("<HHIIHH", original, a)
            fmt_size = size
            checked(size == 16 or original[a + 16:a + 18] == b"\0\0", "Unsupported fmt extra count")
        if kind not in (b"fmt ", b"data"):
            omitted.append({"id_hex": kind.hex(), "payload_bytes": size})
        offset = z + (size % 2)
        count += 1
        checked(count <= 256, "Too many chunks")
    checked(data_start is not None and format_values is not None, "Missing source data/fmt")
    tag, channels, rate, byte_rate, align, bits = format_values
    checked(tag == 1 and channels in (1, 2) and bits == 16 and 8000 <= rate <= 96000 and align == channels * 2 and byte_rate == rate * align, "Source PCM format mismatch")
    checked(report["source"]["format"] == {"channels": channels, "sample_rate_hz": rate, "bits_per_sample": bits,
            "block_align_bytes": align, "format_tag": tag, "original_fmt_bytes": fmt_size}, "Manifest format mismatch")
    checked(report["omitted_source_chunks"] == omitted, "Metadata omission list mismatch")
    pcm = original[data_start:data_end]
    checked(len(pcm) > 0 and len(pcm) % align == 0, "Partial/empty source frames")
    checked(report["source"]["data_offset"] == data_start and report["source"]["data_bytes"] == len(pcm), "Source location mismatch")
    checked(report["source"]["frames"] == len(pcm) // align and report["source"]["pcm_sha256"] == digest(pcm), "Source PCM identity mismatch")
    outputs = report["outputs"]
    checked(type(outputs) is list and 1 <= len(outputs) <= 1000, "Output count invalid")
    checked(len(report["recipe"]["ranges"]) == len(outputs), "Recipe count mismatch")
    expected_files = {"manifest.json"}
    total, reconstructed, cursor, partition = 0, hashlib.sha256(), 0, True
    for i, item in enumerate(outputs, 1):
        name = "excerpt-" + str(i).zfill(4) + ".wav"
        checked(item["filename"] == name and item["ordinal"] == i, "Unsafe filename or order")
        path = packet / name
        checked(not path.is_symlink(), "Verifier does not follow output symlinks")
        expected_files.add(name)
        start, end = item["source_start_frame"], item["source_end_frame_exclusive"]
        checked(type(start) is int and type(end) is int and 0 <= start < end <= len(pcm) // align, "Invalid source range")
        selected = report["recipe"]["ranges"][i - 1]
        checked(selected["start"] == start and selected["end"] == end and selected["label"] == item["label"], "Recipe mapping mismatch")
        expected = pcm[start * align:end * align]
        file_data = bounded(path, 64 * 1024 * 1024)
        checked(len(file_data) == item["file_bytes"] == 44 + len(expected), "Output length mismatch")
        checked(digest(file_data) == item["file_sha256"], "Output file hash mismatch")
        with wave.open(str(path), "rb") as handle:
            checked((handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()) == (channels, 2, rate, end - start), "Output parameters mismatch")
            actual = handle.readframes(end - start + 1)
        checked(actual == expected and digest(actual) == item["pcm_sha256"], "Output differs from exact source PCM slice")
        checked(item["source_byte_start"] == data_start + start * align and item["source_byte_end_exclusive"] == data_start + end * align, "Absolute byte mapping mismatch")
        checked(item["frames"] == end - start, "Frame count mismatch")
        checked(item["time_seconds_exact"] == {"start_numerator": start, "end_numerator": end, "denominator": rate}, "Exact time mapping mismatch")
        total += len(file_data)
        checked(total <= 256 * 1024 * 1024, "Output total exceeds bound")
        partition = partition and start == cursor
        cursor = end
        reconstructed.update(actual)
    checked(total == report["total_wav_bytes"], "Output total mismatch")
    checked({p.name for p in packet.iterdir()} == expected_files, "Unexpected or missing packet files")
    partition = partition and cursor == len(pcm) // align
    if partition:
        checked(reconstructed.hexdigest() == digest(pcm), "Partition reconstruction mismatch")
    return {"verified_excerpts": len(outputs), "pcm_bytes_match_source": True, "complete_partition_reconstructs_source": True if partition else None,
            "limit": "Byte/source-range consistency only, not authenticity, audibility, meaning or a review verdict."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("packet")
    args = parser.parse_args()
    try:
        result = check(Path(args.source), Path(args.packet))
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, struct.error, wave.Error, EOFError) as error:
        print("VERIFICATION FAILED: " + ascii(str(error)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
