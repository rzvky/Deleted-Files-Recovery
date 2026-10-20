# Contributing

Bug reports and pull requests are welcome.

## Reporting a problem

The most useful report includes:

- Windows version and Python version (`python --version`)
- The exact command you ran and the full console output
- Whether the drive is NTFS, its size, and roughly when the files were deleted
- `fsutil fsinfo ntfsinfo D:` output (replace `D:`) if the scan fails before listing anything

Please do not attach disk images or recovered files to public issues.

## Running the tests

The test suite builds a small NTFS image with `ntfs-3g`, deletes files from it, and
checks the tool restores them byte-for-byte.

```
sudo apt install ntfs-3g            # Debian/Ubuntu; needs FUSE
sudo tests/make_image.sh /tmp/ntfs-test
python3 -m pytest tests/ -v
```

On Windows or macOS you can still run the unit tests (no image needed); the
image-based tests skip themselves if `test.img` is absent.

## Ground rules for changes

- The source volume must stay read-only. Any change that opens it with a mode
  other than `"rb"`, or calls anything that writes through the volume handle, will
  not be merged.
- Keep it a single file with no third-party imports. Being able to copy one `.py`
  onto a machine with a dying drive and run it is the whole point.
- Python 3.8 compatibility, since that is what many Windows 10 machines still have.
- Run `pyflakes ntfs_recover.py` before opening a PR.

## Ideas that would be good first contributions

- FAT32 / exFAT directory-entry parsing (currently carving only on those volumes)
- More carving signatures with proper footer detection (HEIC, CR2/NEF raw, OGG)
- A `--list-only` mode that dumps the table to CSV and exits
- A Tkinter front end that wraps the same `NTFS` / `Carver` classes
