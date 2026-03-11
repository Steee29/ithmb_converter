# .ithmb Converter - iPhone iOS 1.x

Tool to extract photos from `.ithmb` files recovered from an iPhone 2G running iOS 1.1.4.

## Quick Start

```bash
pip install Pillow
python main.py
```

Place the `.ithmb` files and the `Photo Database` inside the `input_ithmb/` folder.
Extracted photos will be saved to `converted_photos/`.

## Options

| Flag           | Default            | Description                            |
| -------------- | ------------------ | -------------------------------------- |
| `--format ID`  | `3008`             | Format ID to extract (see table below) |
| `--format 0`   | -                  | Extract all formats                    |
| `--input DIR`  | `input_ithmb`      | Folder containing the source files     |
| `--output DIR` | `converted_photos` | Output folder                          |

Examples:

```bash
python main.py                          # full-size photos only
python main.py --format 3009            # medium thumbnails
python main.py --format 0               # everything
python main.py --input /path/to/dump    # custom input folder
```

## Image Formats (iOS 1.x)

| Format ID | Dimensions  | Pixel Data    | Slot Size     | Padding | Usage               |
| --------- | ----------- | ------------- | ------------- | ------- | ------------------- |
| 3004      | 55x55       | 6,050 B       | 8,192 B       | 2,142 B | Micro thumbnail     |
| **3008**  | **640x480** | **614,400 B** | **614,400 B** | **0**   | **Full-size photo** |
| 3009      | 120x160     | 38,400 B      | 40,960 B      | 2,560 B | Medium thumbnail    |
| 3011      | 75x75       | 11,250 B      | 12,640 B      | 1,390 B | Small thumbnail     |

- Pixel format: **BGR;15** (15-bit, 5-5-5, little-endian, 2 bytes/pixel)
- Files are named `F{format_id}_{number}.ithmb` (e.g. `F3008_1.ithmb`)
- F3008 photos are landscape (640 wide, 480 tall)

## How It Works

### 1. Photo Database (primary method)

The script looks for the `Photo Database` file in the input folder and parses its binary structure to obtain the exact offset and size of each photo.

The database uses the `mhXX` structure (same family as iTunesDB):

```
mhfd (file header)
  mhsd type=1 (image list)
    mhli (num_items = N photos)
      mhii (image item, num_children = M formats)
        mhod type=2
          mhni (offset, size, width, height, format_id)
            mhod type=3 (filename UTF-16LE, e.g. ":Thumbs:F3008_1.ithmb")
        mhod type=2
          mhni ...
        ...
  mhsd type=2 (album list)
    mhla ...
  mhsd type=3 (format definitions)
    mhlf (num_items = 4)
      mhif (format_id, slot_size)
      mhif ...
```

#### mhXX Header Layout

All `mhXX` blocks start with:

- `[0:4]` magic (e.g. `mhfd`, `mhsd`, `mhii`, ...)
- `[4:8]` header_len (uint32 LE) — header size, offset to first child
- `[8:12]` total_len (uint32 LE) — total size including children, offset to next sibling

**Exception**: list blocks (`mhli`, `mhlf`, `mhla`) have `num_items` at offset 8 instead of `total_len`.

#### mhni Layout (thumbnail info)

| Offset | Type    | Field                               |
| ------ | ------- | ----------------------------------- |
| 0      | char[4] | `"mhni"`                            |
| 4      | uint32  | header_len                          |
| 8      | uint32  | total_len                           |
| 12     | uint32  | unknown/flag                        |
| 16     | uint32  | format_id                           |
| 20     | uint32  | img_offset (within the .ithmb file) |
| 24     | uint32  | img_size (bytes)                    |
| 28     | uint32  | unused                              |
| 32     | uint16  | img_width                           |
| 34     | uint16  | img_height                          |

#### mhif Layout (format definition)

| Offset | Type    | Field            |
| ------ | ------- | ---------------- |
| 0      | char[4] | `"mhif"`         |
| 4      | uint32  | header_len (124) |
| 8      | uint32  | total_len (124)  |
| 12     | uint32  | unknown          |
| 16     | uint32  | format_id        |
| 20     | uint32  | slot_size        |

### 2. Fallback (without database)

If the Photo Database is missing or unreadable, the script:

1. Extracts the format ID from the filename using the regex `F(\d{4})_\d+\.ithmb`
2. Uses the hardcoded `KNOWN_FORMATS` table for width, height, and slot_size
3. Splits the file into fixed-size slots
4. Skips blank images (all pixels identical) to filter out empty/deleted slots

### 3. Pixel Decoding

```python
img = Image.frombytes("RGB", (width, height), chunk, "raw", "BGR;15")
```

A single line leveraging Pillow's C-level decoder. BGR;15 = 15-bit color, layout `xBBBBBGGGGGRRRRR` in each little-endian uint16.

## Notes and Gotchas

- **Paths in the database use Mac format**: `:Thumbs:F3008_1.ithmb`. The script extracts only the final filename.
- **The offset in mhni is relative** to the individual `.ithmb` file, not to the database.
- **F3008 photos are landscape** (640x480), NOT portrait (480x640). Using 480x640 causes 3x horizontal repetition in the resulting image.
- **Deleted slots**: the database skips them; in fallback mode they are filtered out by blank-detection (`getextrema`).
- **No headers in .ithmb files**: pixel data starts at offset 0, slots are contiguous.

## Typical File Structure

```
ithmb_converter/
  main.py                          # this script
  input_ithmb/                     # place source files here
    Photo Database                 # Apple binary database
    F3004_1.ithmb                  # micro thumbnails 55x55
    F3008_1.ithmb                  # full-size photos 640x480 (slots 1-854)
    F3008_2.ithmb                  # full-size photos 640x480 (slots 855-1708)
    F3008_3.ithmb                  # full-size photos 640x480 (slots 1709-2562)
    F3008_4.ithmb                  # full-size photos 640x480 (slots 2563-3318)
    F3009_1.ithmb                  # medium thumbnails 120x160
    F3011_1.ithmb                  # small thumbnails 75x75
  converted_photos/                # output folder
```

## Requirements

- Python 3.6+
- Pillow (`pip install Pillow`)

## Troubleshooting

1. **Photos split in half + ghosting**: caused by wrong dimensions (320x480 instead of 640x480). Each 614400-byte slot was being read as 2 images of 307200 bytes.
2. **3x horizontal repetition**: caused by swapped width/height (480x640 instead of 640x480). The 480px row read 3/4 of the actual 640px row, causing misalignment.
3. **Photo Database parser not finding records**: list blocks (`mhli`, `mhlf`) have `num_items` at offset 8, not `total_len`. Also, format definitions use `mhlf` (not `mhli`), and field offsets in `mhif`/`mhni` were wrong.
