"""
.ithmb file converter for iPhone (iOS 1.x)
=============================================
Converts .ithmb files recovered from old iPhones into JPG images.
Uses the Photo Database to read correct offsets and sizes,
or falls back to hardcoded format IDs from the filename.

Instructions:
1. Install Pillow:  pip install Pillow
2. Place this script in the folder with the .ithmb files and the Photo Database
3. Run:  python main.py
4. The images will appear in the "converted_photos" folder
"""

import argparse
import os
import re
import struct
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow library not found.")
    print("Install it with:  pip install Pillow")
    sys.exit(1)

# ── Known formats for iOS 1.x ────────────────────────────────────────────────

KNOWN_FORMATS = {
    3004: {"width": 55, "height": 55, "slot_size": 8192},
    3008: {"width": 640, "height": 480, "slot_size": 614400},
    3009: {"width": 120, "height": 160, "slot_size": 40960},
    3011: {"width": 75, "height": 75, "slot_size": 12640},
}

OUTPUT_FOLDER = "converted_photos"

# ── Photo Database parser (mhXX structure) ────────────────────────────────────

def read_mhxx_header(data, offset):
    """Read a generic mhXX header. Returns (magic, header_len, total_len) or None."""
    if offset + 12 > len(data):
        return None
    magic = data[offset:offset + 4]
    if not magic.startswith(b"mh"):
        return None
    header_len = struct.unpack_from("<I", data, offset + 4)[0]
    total_len = struct.unpack_from("<I", data, offset + 8)[0]
    return magic, header_len, total_len


def parse_format_definitions(data, mhsd_offset, mhsd_total_len):
    """
    Inside an mhsd type=3, search for mhif to extract format_id -> slot_size.
    Structure: mhsd -> mhlf -> mhif * N
    Note: mhlf has num_items at offset 8 (not total_len like mhsd/mhfd).
    """
    formats = {}
    end = mhsd_offset + mhsd_total_len
    hdr = read_mhxx_header(data, mhsd_offset)
    if not hdr:
        return formats
    _, mhsd_hdr_len, _ = hdr
    child_offset = mhsd_offset + mhsd_hdr_len

    # mhlf header: magic(4) + header_len(4) + num_items(4)
    hdr = read_mhxx_header(data, child_offset)
    if not hdr or hdr[0] not in (b"mhli", b"mhlf"):
        return formats
    _, list_hdr_len, num_items = hdr  # offset 8 = num_items for mhl*

    pos = child_offset + list_hdr_len
    for _ in range(num_items):
        if pos >= end:
            break
        hdr = read_mhxx_header(data, pos)
        if not hdr or hdr[0] != b"mhif":
            break
        _, mhif_hdr_len, mhif_total_len = hdr
        # mhif: offset 16 = format_id (uint32), offset 20 = slot_size (uint32)
        if pos + 24 <= len(data):
            format_id = struct.unpack_from("<I", data, pos + 16)[0]
            slot_size = struct.unpack_from("<I", data, pos + 20)[0]
            formats[format_id] = slot_size
        pos += mhif_total_len

    return formats


def parse_image_records(data, mhsd_offset, mhsd_total_len):
    """
    Inside an mhsd type=1, search for mhii -> mhod type=2 -> mhni (offset, size, w, h)
                                                             -> mhod type=3 (filename)
    Returns a list of dicts with: offset, size, width, height, filename, format_id
    """
    records = []
    end = mhsd_offset + mhsd_total_len
    hdr = read_mhxx_header(data, mhsd_offset)
    if not hdr:
        return records
    _, mhsd_hdr_len, _ = hdr
    child_offset = mhsd_offset + mhsd_hdr_len

    # mhli header: magic(4) + header_len(4) + num_items(4)
    hdr = read_mhxx_header(data, child_offset)
    if not hdr or hdr[0] not in (b"mhli", b"mhlf"):
        return records
    _, mhli_hdr_len, num_images = hdr  # offset 8 = num_items for mhl*

    pos = child_offset + mhli_hdr_len
    for _ in range(num_images):
        if pos >= end:
            break
        hdr = read_mhxx_header(data, pos)
        if not hdr or hdr[0] != b"mhii":
            break
        _, mhii_hdr_len, mhii_total_len = hdr

        # Search for mhod children inside this mhii
        # mhii field @12 = num_children
        child_pos = pos + mhii_hdr_len
        mhii_end = pos + mhii_total_len
        if pos + 16 <= len(data):
            num_children = struct.unpack_from("<I", data, pos + 12)[0]
        else:
            num_children = 0

        for _ in range(num_children):
            if child_pos >= mhii_end:
                break
            hdr2 = read_mhxx_header(data, child_pos)
            if not hdr2:
                break
            magic2, mhod_hdr_len, mhod_total_len = hdr2

            if magic2 == b"mhod":
                if child_pos + 16 <= len(data):
                    mhod_type = struct.unpack_from("<I", data, child_pos + 12)[0]
                else:
                    mhod_type = 0

                if mhod_type == 2:
                    mhni_pos = child_pos + mhod_hdr_len
                    hdr3 = read_mhxx_header(data, mhni_pos)
                    if hdr3 and hdr3[0] == b"mhni":
                        _, mhni_hdr_len, mhni_total_len = hdr3
                        rec = parse_mhni(data, mhni_pos, mhni_hdr_len, mhni_total_len)
                        if rec:
                            records.append(rec)

            child_pos += mhod_total_len

        pos += mhii_total_len

    return records


def parse_mhni(data, mhni_pos, mhni_hdr_len, mhni_total_len):
    """
    Parse an mhni record.
    Layout (after the standard 12-byte mhXX header):
      offset 12: unknown/flag (uint32)
      offset 16: format_id (uint32)
      offset 20: img_offset (uint32)
      offset 24: img_size (uint32)
      offset 28: unused (uint32)
      offset 32: img_width (uint16)
      offset 34: img_height (uint16)
    Then searches for mhod type=3 for the filename.
    """
    if mhni_pos + 36 > len(data):
        return None

    format_id = struct.unpack_from("<I", data, mhni_pos + 16)[0]
    img_offset = struct.unpack_from("<I", data, mhni_pos + 20)[0]
    img_size = struct.unpack_from("<I", data, mhni_pos + 24)[0]
    img_width = struct.unpack_from("<H", data, mhni_pos + 32)[0]
    img_height = struct.unpack_from("<H", data, mhni_pos + 34)[0]

    # Search for mhod type=3 (filename) as child of mhni
    filename = None
    child_pos = mhni_pos + mhni_hdr_len
    mhni_end = mhni_pos + mhni_total_len
    while child_pos < mhni_end:
        hdr = read_mhxx_header(data, child_pos)
        if not hdr:
            break
        magic, ch_hdr_len, ch_total_len = hdr
        if magic == b"mhod":
            if child_pos + 16 <= len(data):
                mhod_type = struct.unpack_from("<I", data, child_pos + 12)[0]
                if mhod_type == 3:
                    # Filename in UTF-16LE after the mhod header
                    str_start = child_pos + ch_hdr_len
                    str_end = child_pos + ch_total_len
                    raw = data[str_start:str_end]
                    try:
                        path_str = raw.decode("utf-16-le").rstrip("\x00")
                        # Mac format: ":Thumbs:F3008_1.ithmb" -> filename only
                        filename = path_str.split(":")[-1]
                        if not filename:
                            filename = Path(path_str).name
                    except Exception:
                        pass
        child_pos += ch_total_len

    if not filename or img_width == 0 or img_height == 0:
        return None

    return {
        "format_id": format_id,
        "offset": img_offset,
        "size": img_size,
        "width": img_width,
        "height": img_height,
        "filename": filename,
    }


def parse_photo_database(db_path):
    """
    Parse the Photo Database with mhXX structure.
    Returns (format_defs, image_records) where:
    - format_defs: dict format_id -> slot_size
    - image_records: list of records with offset, size, width, height, filename
    """
    with open(db_path, "rb") as f:
        data = f.read()

    format_defs = {}
    image_records = []

    # Look for mhfd (file header)
    hdr = read_mhxx_header(data, 0)
    if not hdr or hdr[0] != b"mhfd":
        print("  Warning: Photo Database does not start with mhfd, trying anyway...")
        return format_defs, image_records

    _, mhfd_hdr_len, mhfd_total_len = hdr

    # Iterate over mhsd children
    pos = mhfd_hdr_len
    file_end = min(mhfd_total_len, len(data))
    while pos < file_end:
        hdr = read_mhxx_header(data, pos)
        if not hdr:
            break
        magic, sd_hdr_len, sd_total_len = hdr
        if magic != b"mhsd":
            pos += sd_total_len if sd_total_len > 0 else 1
            continue

        # mhsd type at offset 12
        if pos + 16 <= len(data):
            sd_type = struct.unpack_from("<I", data, pos + 12)[0]
        else:
            sd_type = 0

        if sd_type == 3:
            format_defs = parse_format_definitions(data, pos, sd_total_len)
        elif sd_type == 1:
            image_records = parse_image_records(data, pos, sd_total_len)

        pos += sd_total_len

    return format_defs, image_records


# ── Image extraction ─────────────────────────────────────────────────────────

def is_blank(img):
    """Check if the image is completely uniform (blank)."""
    extrema = img.getextrema()
    return all(lo == hi for lo, hi in extrema)


def extract_with_database(records_by_file, ithmb_dir, output_dir):
    """Extract images using records from the Photo Database."""
    total = sum(len(recs) for recs in records_by_file.values())
    count = 0
    saved = 0

    for filename, recs in sorted(records_by_file.items()):
        filepath = ithmb_dir / filename
        if not filepath.exists():
            # Case-insensitive search
            found = None
            for f in ithmb_dir.iterdir():
                if f.name.lower() == filename.lower():
                    found = f
                    break
            if not found:
                print(f"  File not found: {filename}, skipping {len(recs)} records")
                count += len(recs)
                continue
            filepath = found

        with open(filepath, "rb") as f:
            for rec in recs:
                count += 1
                w, h = rec["width"], rec["height"]
                pixel_bytes = w * h * 2

                f.seek(rec["offset"])
                chunk = f.read(pixel_bytes)
                if len(chunk) < pixel_bytes:
                    continue

                try:
                    img = Image.frombytes("RGB", (w, h), chunk, "raw", "BGR;15")
                except Exception:
                    continue

                if is_blank(img):
                    continue

                out_name = f"{Path(filename).stem}_{count:05d}.jpg"
                img.save(output_dir / out_name, "JPEG", quality=95)
                saved += 1
                print(f"\r  [{count}/{total}] Saved: {saved}", end="", flush=True)

    print()
    return saved


def extract_fallback(ithmb_dir, output_dir, target_format=3008):
    """Extract images with hardcoded format ID from filename."""
    fmt_re = re.compile(r"F(\d{4})_\d+\.ithmb", re.IGNORECASE)
    ithmb_files = sorted(ithmb_dir.glob("*.ithmb"))

    if not ithmb_files:
        return 0

    # Filter by target format
    files_to_process = []
    for f in ithmb_files:
        m = fmt_re.match(f.name)
        if m:
            fid = int(m.group(1))
            if fid == target_format and fid in KNOWN_FORMATS:
                files_to_process.append((f, KNOWN_FORMATS[fid]))
        elif target_format == 0:
            # If --format=all and the name doesn't match, try 3008
            files_to_process.append((f, KNOWN_FORMATS[3008]))

    if not files_to_process:
        print(f"  No files found for format {target_format}")
        return 0

    saved = 0
    total_slots = 0
    for filepath, fmt in files_to_process:
        file_size = filepath.stat().st_size
        total_slots += file_size // fmt["slot_size"]

    count = 0
    for filepath, fmt in files_to_process:
        w, h = fmt["width"], fmt["height"]
        slot_size = fmt["slot_size"]
        pixel_bytes = w * h * 2
        file_size = filepath.stat().st_size
        n_slots = file_size // slot_size

        print(f"  {filepath.name}: {n_slots} slots ({w}x{h})")

        with open(filepath, "rb") as f:
            for i in range(n_slots):
                count += 1
                f.seek(i * slot_size)
                chunk = f.read(pixel_bytes)
                if len(chunk) < pixel_bytes:
                    continue

                try:
                    img = Image.frombytes("RGB", (w, h), chunk, "raw", "BGR;15")
                except Exception:
                    continue

                if is_blank(img):
                    continue

                out_name = f"{filepath.stem}_{i+1:05d}.jpg"
                img.save(output_dir / out_name, "JPEG", quality=95)
                saved += 1
                print(f"\r  [{count}/{total_slots}] Saved: {saved}", end="", flush=True)

        print()

    return saved


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=".ithmb -> JPG converter for iPhone iOS 1.x"
    )
    parser.add_argument(
        "--format", type=int, default=3008,
        help="Format ID to extract (default: 3008 = full-size 640x480). "
             "Values: 3004, 3008, 3009, 3011. Use 0 for all."
    )
    parser.add_argument(
        "--input", type=str, default="input_ithmb",
        help="Folder with .ithmb files and Photo Database (default: input_ithmb)"
    )
    parser.add_argument(
        "--output", type=str, default=OUTPUT_FOLDER,
        help="Output folder (default: converted_photos)"
    )
    args = parser.parse_args()

    ithmb_dir = Path(args.input).resolve()
    output_dir = Path(args.output)
    output_dir.mkdir(exist_ok=True)

    print("=" * 60)
    print("  .ithmb -> JPG Converter  (iOS 1.x)")
    print("=" * 60)
    print(f"\nInput folder:  {ithmb_dir}")
    print(f"Output folder: {output_dir.resolve()}")

    if args.format == 0:
        print("Format: ALL")
    elif args.format in KNOWN_FORMATS:
        fmt = KNOWN_FORMATS[args.format]
        print(f"Format: {args.format} ({fmt['width']}x{fmt['height']})")
    else:
        print(f"Format: {args.format}")
    print()

    # Look for the Photo Database
    db_path = None
    for name in ["Photo Database", "PhotoDatabase"]:
        candidate = ithmb_dir / name
        if candidate.exists():
            db_path = candidate
            break
    if not db_path:
        # Search recursively
        candidates = list(ithmb_dir.rglob("Photo Database")) + \
                     list(ithmb_dir.rglob("PhotoDatabase"))
        if candidates:
            db_path = candidates[0]

    saved = 0

    if db_path:
        print(f"Photo Database found: {db_path}")
        try:
            format_defs, image_records = parse_photo_database(db_path)
            print(f"  Defined formats: {len(format_defs)}")
            for fid, slot in sorted(format_defs.items()):
                known = KNOWN_FORMATS.get(fid, {})
                w = known.get("width", "?")
                h = known.get("height", "?")
                print(f"    {fid}: slot={slot}, {w}x{h}")
            print(f"  Image records: {len(image_records)}")

            if image_records:
                # Filter by requested format
                if args.format != 0:
                    filtered = [r for r in image_records if r["format_id"] == args.format]
                    print(f"  Records for format {args.format}: {len(filtered)}")
                else:
                    filtered = image_records

                # Group by filename
                records_by_file = {}
                for rec in filtered:
                    records_by_file.setdefault(rec["filename"], []).append(rec)

                print(f"\nExtracting from {len(records_by_file)} files...\n")
                saved = extract_with_database(records_by_file, ithmb_dir, output_dir)
            else:
                print("\n  No records found in database, using fallback.\n")
                saved = extract_fallback(ithmb_dir, output_dir, args.format)

        except Exception as e:
            print(f"\n  Error parsing the database: {e}")
            print("  Using fallback with format ID from filename.\n")
            saved = extract_fallback(ithmb_dir, output_dir, args.format)
    else:
        print("Photo Database not found, using fallback with format ID from filename.\n")
        saved = extract_fallback(ithmb_dir, output_dir, args.format)

    print()
    print("=" * 60)
    print(f"  COMPLETED: {saved} images saved")
    print(f"  Folder: {output_dir.resolve()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
