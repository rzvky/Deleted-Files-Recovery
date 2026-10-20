# Deleted-Files-Recovery

**Free, open source tool to recover deleted files from a hard drive on Windows 10 and 11.
Read-only, no installation, no file-size limit, no "pro version".**

[![tests](https://github.com/rzvky/Deleted-Files-Recovery/actions/workflows/tests.yml/badge.svg)](https://github.com/rzvky/Deleted-Files-Recovery/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

Deleted a folder of photos, emptied the Recycle Bin, or lost files from an external
HDD? As long as the drive is NTFS and the space has not been reused, the data is
usually still there. This tool finds it, shows you the original file names, folders
and dates, lets you preview images, and restores only what you pick into your
Downloads folder.

The drive you are recovering from is opened **read-only**. The script never writes
to it, so running it cannot make a bad situation worse. It is one Python file with
zero dependencies that you can read from top to bottom.

```
    # | File name           | Approx. deleted  | Type |     Size | Status      | Original folder
--------------------------------------------------------------------------------------------------
    1 | IMG_0001.jpg        | 2026-10-12 15:57 | jpg  |   5.3 KB | OK          | Photos\2024
    2 | IMG_0002.jpg        | 2026-10-12 15:57 | jpg  |  29.9 KB | OK          | Photos\2024
    3 | Report Q3.pdf       | 2026-10-12 15:57 | pdf  | 293.1 KB | OK          | Docs
    4 | Thesis draft.docx   | 2026-10-12 15:57 | docx |    412 B | OK          | Docs
    5 | VID-20240101.mp4    | 2026-10-12 15:57 | mp4  |   1.9 MB | OVERWRITTEN | Video

recover> f jpg download
  Restore 2 file(s), 35.2 KB total, into C:\Users\you\Downloads\Recovered_D_2026-10-12_160219 ? [y/N] y
  [1] IMG_0001.jpg  -> ...\Recovered_D_2026-10-12_160219\Photos\2024\IMG_0001.jpg  (5.3 KB)
  [2] IMG_0002.jpg  -> ...\Recovered_D_2026-10-12_160219\Photos\2024\IMG_0002.jpg  (29.9 KB)
```

## Contents

- [What it can recover](#what-it-can-recover)
- [Why another recovery tool](#why-another-recovery-tool)
- [Requirements](#requirements)
- [Quick start: recover deleted files in 5 steps](#quick-start-recover-deleted-files-in-5-steps)
- [Commands](#commands)
- [Command-line options](#command-line-options)
- [Where recovered files go](#where-recovered-files-go)
- [Reading the Status column](#reading-the-status-column)
- [How it works](#how-it-works)
- [Limits](#limits)
- [FAQ](#faq)
- [Tests](#tests)
- [Contributing](#contributing)

## What it can recover

- Files deleted with Shift+Delete or after emptying the Recycle Bin
- Files lost from an external hard drive, USB HDD or second internal partition
- Photos (JPG, PNG, GIF), videos (MP4, MOV), documents (PDF, DOCX, XLSX, PPTX),
  archives (ZIP, 7z, RAR), audio (MP3), and any other file type that still has
  its NTFS record
- Original file names, folder paths and timestamps, when the record is intact
- Files whose record is gone, through signature-based file carving (no names)

It works on NTFS volumes, which is what Windows uses for internal drives and
most external hard drives. For FAT32 and exFAT (many USB sticks and SD cards)
it falls back to carving only.

## Why another recovery tool

Most free recovery software caps the amount you can restore and asks you to
pay for the rest, or hides what it is doing to your disk. When a drive holds
files you care about, two things matter: nothing must write to it, and you
should see exactly what was found before deciding what to pull off. This tool
is built around those two rules and nothing else. No installer, no background
service, no upsell, no telemetry.

## Requirements

- Windows 10 or 11
- Python 3.8 or newer from [python.org](https://www.python.org/downloads/)
  (tick "Add python.exe to PATH" during install)
- Administrator rights: Windows only allows raw disk reads from an elevated
  process. The script asks for this itself.
- The drive being recovered should be NTFS. Check in Explorer: right-click the
  drive, Properties, "File system".
- BitLocker drives must be unlocked first.
- Free space in your Downloads folder for what you intend to restore.

No `pip install` is needed. The script uses only the Python standard library.

## Quick start: recover deleted files in 5 steps

1. **Stop using the affected drive.** Every write, including a browser cache or
   a Windows update, can overwrite deleted data for good. Do not save or install
   anything onto it, including this script.

2. **Download `ntfs_recover.py`** from the
   [latest release](https://github.com/rzvky/Deleted-Files-Recovery/releases)
   or clone the repo:

   ```
   git clone https://github.com/rzvky/Deleted-Files-Recovery.git
   ```

3. **Run it as administrator.** Either double-click `ntfs_recover.py` (it asks for
   UAC and relaunches itself), or open Command Prompt / Terminal as administrator
   and run:

   ```
   python ntfs_recover.py
   ```

4. **Enter the drive letter** (for example `D`). The scan of the file table takes
   seconds to a couple of minutes. Say `n` to the signature scan the first time;
   come back to it only if a file you need is missing from the list.

5. **Find and restore.** Type `f jpg` to filter, `g` to see thumbnails, `p 12` to
   preview one file, then `1,4,7-12`, `all`, or `f VID- download` to restore.
   Files land in `Downloads\Recovered_D_<date>\` with their original folders.

## Commands

At the `recover>` prompt:

**Restore**

```
5                 restore entry 5
1,4,7-12          restore 1, 4 and 7 through 12
all               restore everything currently listed
download  (or d)  same as all
f VID- download   filter to names containing VID- and restore all of them
f mp4 restore     same thing; restore, download and d are interchangeable
```

**Find**

```
f jpg             entries whose type, name, folder, status or source contains "jpg"
f Photos          files that lived under a folder called Photos
f ok              only entries whose data is still untouched
f carve           only files found by the signature scan
f                 clear the filter
s                 show the table (first 400 rows)
s all             show everything
```

**Look before restoring**

```
p 12              copy entry 12 to Downloads\...\_preview\ and open it
g                 build an HTML thumbnail gallery of every image in the list and open it
```

The gallery labels each thumbnail with its index so you can type the numbers
back at the prompt. Filter first (`f jpg`, `f 2024`) if the list is long.

**Other**

```
h                 help
q                 quit
Ctrl+C            during the signature scan: stop and keep what was found so far
```

Index numbers refer to the table as currently shown, so after a filter the
numbering restarts at 1. Every restore shows the file count and total size and
asks for `y` before writing anything.

## Command-line options

```
python ntfs_recover.py [--drive D] [--carve | --no-carve] [--min-size BYTES]
                       [--out FOLDER] [--image FILE] [--version]
```

| Option | Effect |
|---|---|
| `--drive D` | skip the drive-letter prompt |
| `--carve` | run the signature scan without asking |
| `--no-carve` | never run the signature scan |
| `--min-size N` | ignore carved fragments smaller than N bytes (default 1024) |
| `--out FOLDER` | write somewhere other than Downloads. Refused if it is on the drive being scanned |
| `--image FILE` | scan a raw disk image (`.img`, `.dd`) instead of a live drive. Works on any OS, no admin needed |

## Where recovered files go

```
Downloads\Recovered_D_2026-10-12_160219\
    Photos\2024\IMG_0001.jpg          original folder structure, recreated
    Docs\Report Q3.pdf
    _carved\carved_0000102400.jpg     signature-scan finds, named by sector
    _gallery\gallery_1.html           only if you used g
    _preview\                         only if you used p
```

Name clashes get ` (2)`, ` (3)` appended rather than overwriting. If your
Downloads folder is on the drive you are scanning (for example scanning `C:`),
the tool refuses to run; use `--out` to point at another drive.

## Reading the Status column

| Status | Meaning |
|---|---|
| `OK` | the file's clusters are still marked free; nothing has been written over them |
| `PARTIAL` | some clusters now belong to a live file; the restored file will be damaged |
| `OVERWRITTEN` | all clusters have been re-used; expect garbage |
| `CARVED` | found by signature scan; good if the file was stored in one piece |

"Approx. deleted" is the file record's own last-change time. NTFS does not store
a deletion timestamp, but a normal delete updates this field at that moment, so
it is usually right.

## How it works

1. Read the boot sector for sector size, cluster size, MFT location and record size.
2. Read MFT record 0 to find the MFT's own data runs, then walk every record,
   applying the update-sequence fixup and skipping torn records.
3. For each record with the in-use flag cleared, take timestamps from
   `$STANDARD_INFORMATION`, the name and parent reference from `$FILE_NAME`
   (preferring the long name over the 8.3 one), and either the resident content
   or the cluster run list from `$DATA`.
4. Rebuild the folder path by following parent references. If a parent record's
   sequence number has moved on, that record was re-used and the path shows `?`
   rather than a wrong folder.
5. Read `$Bitmap` and sample the file's clusters to decide `OK` / `PARTIAL` /
   `OVERWRITTEN`.
6. The signature scan reads the partition in 16 MB chunks, searches for magic
   bytes, skips clusters the bitmap marks as allocated, and cuts each hit at its
   footer: JPEG `FF D9`, PNG `IEND`, PDF `%%EOF`, ZIP end-of-central-directory,
   MP4 box walk, 7z header length. Types without a reliable footer get a
   fixed-size slab.
7. Restoring streams the cluster runs straight to a file in the output folder.

All reads go through one `RawVolume` class that opens the device with `"rb"` and
aligns every read to the sector size. There is no other handle to the device
anywhere in the file.

## Limits

- A deleted file whose record has already been re-used will not appear in the
  main list. The signature scan may still find its contents, without the name.
- The signature scan assumes files are contiguous. Fragmented files come back corrupt.
- Records with `$ATTRIBUTE_LIST` (heavily fragmented files) are flagged and may
  restore incompletely.
- FAT32 / exFAT: no directory parsing yet, signature scan only, and because those
  volumes have no cluster map here, results include live files too.
- SSDs: TRIM usually zeroes deleted blocks within minutes. The tool runs, but
  `OK` entries may come back empty. It is written for spinning hard drives.
- If the UAC prompt asks for a different administrator's password, Downloads
  resolves to that account. The output path is printed at the end either way.

## FAQ

**Can I recover files after emptying the Recycle Bin?**
Yes, if the drive is NTFS and the space has not been reused. Emptying the bin
only clears the file's "in use" flag; the data stays until something overwrites
it. Stop using the drive and run the scan as soon as possible.

**Can I recover permanently deleted files (Shift+Delete)?**
Same answer. Shift+Delete skips the Recycle Bin but does the same thing to the
file record.

**Does it work on an external hard drive or USB HDD?**
Yes, as long as Windows shows it with a drive letter and it is NTFS.

**Does it work on a USB flash drive or SD card?**
Those are usually FAT32 or exFAT. The tool will run the signature scan on them,
which recovers photos and documents but not file names.

**Does it work on an SSD?**
Rarely. SSDs erase deleted blocks in the background (TRIM), often within minutes.
You can try, but expect little.

**Does it work on drive C:?**
It can scan it, but your Downloads folder is on C:, so writing there would
overwrite the files you want back. Run with `--out E:\recovered` (any other
drive) or, better, take the disk out and scan it from another machine.

**Will it damage my drive or my files?**
No. The drive is opened read-only and the script contains no code path that
writes to it. You can verify this yourself: search `ntfs_recover.py` for `"wb"`
and you will find only writes to the output folder.

**Is there a file size or file count limit?**
No.

**It says "Access denied" or "Run as Administrator".**
Right-click Command Prompt or Terminal, choose "Run as administrator", then run
the script from there.

**It says "Not an NTFS volume".**
The drive is FAT32, exFAT, ReFS, or locked by BitLocker. Unlock BitLocker in
Windows, or use `--carve` for FAT32/exFAT.

**How long does it take?**
The file-table scan: seconds to a few minutes. The optional signature scan reads
the whole partition, about an hour per 400 to 500 GB on a mechanical drive.

**Can I recover files from a disk image instead of a live drive?**
Yes: `python ntfs_recover.py --image disk.img`. This also works on Linux and macOS.

## Tests

`tests/` builds a 64 MB NTFS image with `ntfs-3g`, writes files with nested
folders, spaces and Unicode names, deletes most of them, writes a newer file over
some of the freed space, and then checks that:

- every deleted name is listed and no live file is
- every entry reported `OK` restores byte-for-byte (SHA-256)
- `f VID- download` restores exactly the matching file
- the signature scan returns exact copies of the deleted JPEG, PNG and PDF
- the gallery page is generated
- the source image's hash is unchanged after a full run

```
sudo apt install ntfs-3g
sudo tests/make_image.sh /tmp/ntfs-test
python3 -m pytest tests/ -v
```

The unit tests run without the image; the image tests skip themselves if it is
missing. CI runs everything on Ubuntu for Python 3.8 and 3.12.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The one non-negotiable is that the
source drive stays read-only.

## License

MIT. See [LICENSE](LICENSE).

---

*Keywords: recover deleted files, undelete, data recovery, NTFS recovery, restore
deleted files Windows 10, Windows 11, recover permanently deleted files, recover
files after emptying Recycle Bin, external hard drive recovery, deleted photo
recovery, file carving, MFT parser, free open source recovery tool, Python.*
