#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Deleted-Files-Recovery: read-only undelete for NTFS volumes on Windows.

Scans the $MFT for deleted entries, optionally carves free space for known
file signatures, and restores what you pick into your Downloads folder.
The source volume is opened read-only and is never written to.

Usage (run from an elevated prompt, or let it ask for UAC):

    python ntfs_recover.py
    python ntfs_recover.py --drive D --no-carve
    python ntfs_recover.py --image some_disk.img --out ./recovered

See README.md for the full command reference.
"""

__version__ = "1.1.0"
__author__ = "rzvky"
__license__ = "MIT"
__url__ = "https://github.com/rzvky/Deleted-Files-Recovery"


import argparse
import ctypes
import datetime as _dt
import html
import os
import re
import struct
import subprocess
import sys
import time
from collections import namedtuple

IS_WINDOWS = os.name == "nt"
VERSION = __version__

# --------------------------------------------------------------------------
#  Small helpers
# --------------------------------------------------------------------------

def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def filetime_to_dt(ft):
    """Windows FILETIME (100ns since 1601-01-01 UTC) -> local datetime or None."""
    if not ft or ft < 116444736000000000:
        return None
    try:
        return _dt.datetime.fromtimestamp((ft - 116444736000000000) / 10_000_000)
    except (OverflowError, OSError, ValueError):
        return None


def fmt_dt(d):
    return d.strftime("%Y-%m-%d %H:%M") if d else "-"


def safe_filename(name, fallback="unnamed"):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or fallback


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = "%s (%d)%s" % (base, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1


def parse_selection(text, max_index):
    """'1,3,5-9'  -> sorted set of ints within 1..max_index.  Raises ValueError."""
    picked = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError("Cannot understand '%s'" % part)
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a > b:
            a, b = b, a
        if a < 1 or b > max_index:
            raise ValueError("'%s' is outside 1-%d" % (part, max_index))
        picked.update(range(a, b + 1))
    if not picked:
        raise ValueError("Nothing selected")
    return sorted(picked)


# --------------------------------------------------------------------------
#  Windows-specific helpers (elevation, Downloads folder)
# --------------------------------------------------------------------------

def is_admin():
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Re-run this script elevated. Returns True if a new process was started."""
    if not IS_WINDOWS:
        return False
    params = " ".join('"%s"' % a for a in [os.path.abspath(sys.argv[0])] + sys.argv[1:])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return rc > 32
    except Exception:
        return False


def downloads_folder():
    """Real Downloads folder via Known Folder API (handles relocated folders)."""
    if IS_WINDOWS:
        try:
            # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}, little-endian GUID layout
            guid = (ctypes.c_ubyte * 16).from_buffer_copy(
                b"\x90\xe2\x4d\x37\x3f\x12\x65\x45\x91\x64\x39\xc4\x92\x5e\x46\x7b")
            path_ptr = ctypes.c_wchar_p()
            res = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
            if res == 0 and path_ptr.value:
                p = path_ptr.value
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                return p
        except Exception:
            pass
        p = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Downloads")
        return p
    return os.path.join(os.path.expanduser("~"), "Downloads")


# --------------------------------------------------------------------------
#  Read-only raw volume access
# --------------------------------------------------------------------------

class RawVolume:
    r"""Sector-aligned read-only reader for \\.\X: or an image file."""

    def __init__(self, path, is_device):
        self.path = path
        self.is_device = is_device
        # 4096 is a multiple of every real sector size (512e and 4Kn), so the very
        # first reads are safe before the boot sector tells us the true value.
        self.sector = 4096
        # 'rb' = read only. Nothing in this class ever calls write().
        self.fh = open(path, "rb", buffering=0)
        self.size = self._detect_size()
        self.bytes_read = 0

    def _detect_size(self):
        if not self.is_device:
            return os.path.getsize(self.path)
        if IS_WINDOWS:
            try:
                import msvcrt
                from ctypes import wintypes
                handle = msvcrt.get_osfhandle(self.fh.fileno())
                IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
                out = ctypes.c_longlong(0)
                ret = wintypes.DWORD(0)
                ok = ctypes.windll.kernel32.DeviceIoControl(
                    wintypes.HANDLE(handle), IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                    ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(ret), None)
                if ok and out.value > 0:
                    return out.value
            except Exception:
                pass
        return None  # unknown; NTFS boot sector will tell us

    def read(self, offset, length):
        """Read arbitrary (offset, length) using aligned sector I/O."""
        if length <= 0:
            return b""
        if self.size is not None:
            if offset >= self.size:
                return b""
            length = min(length, self.size - offset)
        s = self.sector
        a_off = (offset // s) * s
        a_end = ((offset + length + s - 1) // s) * s
        self.fh.seek(a_off)
        data = self.fh.read(a_end - a_off)
        self.bytes_read += len(data)
        return data[offset - a_off: offset - a_off + length]

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
#  NTFS structures
# --------------------------------------------------------------------------

ATTR_STANDARD_INFO = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_END = 0xFFFFFFFF

FLAG_IN_USE = 0x01
FLAG_DIRECTORY = 0x02

DataRun = namedtuple("DataRun", "lcn length")  # lcn None = sparse


class Entry:
    """One finding: either an MFT record or a carved file."""
    __slots__ = ("source", "mft_no", "seq", "name", "path", "size", "alloc_size",
                 "created", "modified", "mft_changed", "is_dir", "resident_data",
                 "runs", "ext", "status", "carve_offset", "note", "parent_ref")

    def __init__(self):
        self.source = "MFT"
        self.mft_no = None
        self.seq = 0
        self.name = ""
        self.path = ""
        self.size = 0
        self.alloc_size = 0
        self.created = None
        self.modified = None
        self.mft_changed = None
        self.is_dir = False
        self.resident_data = None
        self.runs = []
        self.ext = ""
        self.status = "OK"
        self.carve_offset = None
        self.note = ""
        self.parent_ref = None

    @property
    def display_name(self):
        return self.name

    @property
    def full_path(self):
        return (self.path.rstrip("\\") + "\\" + self.name) if self.path else self.name


class NTFS:
    def __init__(self, vol):
        self.vol = vol
        bs = vol.read(0, 4096)
        if len(bs) < 512 or bs[3:11] != b"NTFS    ":
            raise ValueError("Not an NTFS volume (OEM id = %r)" % bs[3:11])
        self.bytes_per_sector = struct.unpack_from("<H", bs, 11)[0]
        self.sectors_per_cluster = bs[13]
        if self.sectors_per_cluster > 0x80:  # values 0xF1..0xFF mean 2^(256-x)
            self.sectors_per_cluster = 1 << (256 - self.sectors_per_cluster)
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.total_sectors = struct.unpack_from("<Q", bs, 40)[0]
        self.mft_lcn = struct.unpack_from("<Q", bs, 48)[0]
        c = struct.unpack_from("<b", bs, 64)[0]
        self.record_size = (1 << -c) if c < 0 else c * self.cluster_size
        self.volume_size = self.total_sectors * self.bytes_per_sector
        vol.sector = self.bytes_per_sector
        if vol.size is None:
            vol.size = self.volume_size + self.bytes_per_sector
        self.total_clusters = self.volume_size // self.cluster_size
        self.mft_runs = []
        self.mft_size = 0
        self.names = {}      # mft_no -> (name, parent_no, parent_seq, seq, is_dir)
        self.bitmap = None
        self._load_mft_runs()

    # ---- low level ----------------------------------------------------
    def _fixup(self, rec):
        """Apply the update sequence array; returns fixed bytes or None if torn."""
        if len(rec) < 48 or rec[0:4] != b"FILE":
            return None
        usa_off, usa_cnt = struct.unpack_from("<HH", rec, 4)
        if usa_off + usa_cnt * 2 > len(rec) or usa_cnt < 2:
            return None
        usn = rec[usa_off:usa_off + 2]
        rec = bytearray(rec)
        bps = self.bytes_per_sector
        for i in range(1, usa_cnt):
            pos = i * bps - 2
            if pos + 2 > len(rec):
                break
            if bytes(rec[pos:pos + 2]) != usn:
                return None  # torn/corrupt sector
            rec[pos:pos + 2] = rec[usa_off + i * 2: usa_off + i * 2 + 2]
        return bytes(rec)

    @staticmethod
    def decode_runs(buf, off, end):
        runs = []
        lcn = 0
        while off < end:
            hdr = buf[off]
            if hdr == 0:
                break
            ln_sz, off_sz = hdr & 0x0F, hdr >> 4
            off += 1
            if off + ln_sz + off_sz > end or ln_sz == 0:
                break
            length = int.from_bytes(buf[off:off + ln_sz], "little")
            off += ln_sz
            if off_sz == 0:
                runs.append(DataRun(None, length))  # sparse
            else:
                delta = int.from_bytes(buf[off:off + off_sz], "little", signed=True)
                off += off_sz
                lcn += delta
                runs.append(DataRun(lcn, length))
        return runs

    def read_runs(self, runs, size, chunk_cb=None, max_bytes=None):
        """Read non-resident data. Returns bytes, or streams via chunk_cb(bytes)."""
        remaining = size if max_bytes is None else min(size, max_bytes)
        out = [] if chunk_cb is None else None
        cs = self.cluster_size
        for run in runs:
            if remaining <= 0:
                break
            run_bytes = min(run.length * cs, remaining)
            if run.lcn is None:
                pos = 0
                while pos < run_bytes:
                    n = min(4 << 20, run_bytes - pos)
                    data = b"\x00" * n
                    (out.append(data) if out is not None else chunk_cb(data))
                    pos += n
            else:
                start = run.lcn * cs
                pos = 0
                while pos < run_bytes:
                    n = min(8 << 20, run_bytes - pos)
                    data = self.vol.read(start + pos, n)
                    if not data:
                        break
                    (out.append(data) if out is not None else chunk_cb(data))
                    pos += len(data)
            remaining -= run_bytes
        if out is not None:
            return b"".join(out)

    def _load_mft_runs(self):
        rec = self.vol.read(self.mft_lcn * self.cluster_size, self.record_size)
        rec = self._fixup(rec)
        if rec is None:
            raise ValueError("$MFT record 0 is damaged")
        for a in self._attributes(rec):
            if a["type"] == ATTR_DATA and a["name"] == "":
                if a["resident"]:
                    raise ValueError("$MFT data is resident?!")
                self.mft_runs = a["runs"]
                self.mft_size = a["size"]
                break
        if not self.mft_runs:
            raise ValueError("Could not read $MFT data runs")

    def _attributes(self, rec):
        """Yield attribute dicts from a fixed-up record."""
        first = struct.unpack_from("<H", rec, 20)[0]
        off = first
        n = len(rec)
        while off + 16 <= n:
            atype = struct.unpack_from("<I", rec, off)[0]
            if atype == ATTR_END or atype == 0:
                break
            alen = struct.unpack_from("<I", rec, off + 4)[0]
            if alen < 24 or off + alen > n:
                break
            nonres = rec[off + 8]
            name_len = rec[off + 9]
            name_off = struct.unpack_from("<H", rec, off + 10)[0]
            flags = struct.unpack_from("<H", rec, off + 12)[0]
            name = ""
            if name_len:
                try:
                    name = rec[off + name_off: off + name_off + name_len * 2].decode("utf-16-le", "replace")
                except Exception:
                    name = "?"
            a = {"type": atype, "name": name, "resident": not nonres, "flags": flags}
            if not nonres:
                clen, coff = struct.unpack_from("<IH", rec, off + 16)
                a["data"] = rec[off + coff: off + coff + clen] if off + coff + clen <= n else b""
                a["size"] = clen
                a["alloc"] = clen
            else:
                start_vcn, end_vcn = struct.unpack_from("<QQ", rec, off + 16)
                runs_off = struct.unpack_from("<H", rec, off + 32)[0]
                alloc, real, init = struct.unpack_from("<QQQ", rec, off + 40)
                a["size"] = real
                a["alloc"] = alloc
                a["start_vcn"] = start_vcn
                a["runs"] = self.decode_runs(rec, off + runs_off, off + alen)
            yield a
            off += alen

    def record_count(self):
        return self.mft_size // self.record_size

    def iter_records(self, progress=None):
        """Yield (mft_no, raw_record_bytes_or_None) for the whole MFT."""
        cs = self.cluster_size
        rs = self.record_size
        per_cluster = max(1, cs // rs)
        mft_no = 0
        total = self.record_count()
        for run in self.mft_runs:
            if run.lcn is None:
                mft_no += run.length * per_cluster
                continue
            # read in 2 MB chunks
            clusters_left = run.length
            lcn = run.lcn
            while clusters_left > 0:
                take = min(clusters_left, max(1, (2 << 20) // cs))
                buf = self.vol.read(lcn * cs, take * cs)
                for i in range(len(buf) // rs):
                    if mft_no >= total:
                        return
                    yield mft_no, buf[i * rs:(i + 1) * rs]
                    mft_no += 1
                if progress and mft_no % 4096 < per_cluster * take:
                    progress(mft_no, total)
                lcn += take
                clusters_left -= take

    # ---- parsing ------------------------------------------------------
    def parse_record(self, mft_no, raw):
        rec = self._fixup(raw)
        if rec is None:
            return None
        seq = struct.unpack_from("<H", rec, 16)[0]
        flags = struct.unpack_from("<H", rec, 22)[0]
        base_ref = struct.unpack_from("<Q", rec, 32)[0]
        if base_ref & 0xFFFFFFFFFFFF:
            return None  # extension record of another file; skip
        e = Entry()
        e.mft_no = mft_no
        e.seq = seq
        e.is_dir = bool(flags & FLAG_DIRECTORY)
        in_use = bool(flags & FLAG_IN_USE)
        best_ns = 99
        has_attr_list = False
        data_found = False
        for a in self._attributes(rec):
            t = a["type"]
            if t == ATTR_STANDARD_INFO and a["resident"] and len(a["data"]) >= 32:
                cr, mo, mc, ac = struct.unpack_from("<QQQQ", a["data"], 0)
                e.created = filetime_to_dt(cr)
                e.modified = filetime_to_dt(mo)
                e.mft_changed = filetime_to_dt(mc)
            elif t == ATTR_FILE_NAME and a["resident"] and len(a["data"]) >= 66:
                d = a["data"]
                parent_ref = struct.unpack_from("<Q", d, 0)[0]
                nlen = d[64]
                ns = d[65]
                name = d[66:66 + nlen * 2].decode("utf-16-le", "replace")
                # prefer POSIX(0)/Win32(1)/Win32&DOS(3) over DOS(2)
                rank = {2: 3, 0: 1, 1: 0, 3: 0}.get(ns, 2)
                if rank < best_ns:
                    best_ns = rank
                    e.name = name
                    e.parent_ref = (parent_ref & 0xFFFFFFFFFFFF, parent_ref >> 48)
                    if e.size == 0 and not data_found:
                        e.size = struct.unpack_from("<Q", d, 48)[0]
            elif t == ATTR_DATA and a["name"] == "":
                data_found = True
                e.size = a["size"]
                e.alloc_size = a["alloc"]
                if a["resident"]:
                    e.resident_data = a["data"]
                else:
                    e.runs = a["runs"]
            elif t == ATTR_ATTRIBUTE_LIST:
                has_attr_list = True
        if not e.name:
            return None
        if has_attr_list and not e.resident_data and not e.runs:
            e.note = "fragmented record (attribute list); data may be incomplete"
        e.ext = os.path.splitext(e.name)[1].lower().lstrip(".")
        return e, in_use

    def scan(self, progress=None):
        """Walk the MFT. Returns list of deleted Entry objects (files only)."""
        deleted = []
        for mft_no, raw in self.iter_records(progress):
            parsed = self.parse_record(mft_no, raw)
            if not parsed:
                continue
            e, in_use = parsed
            self.names[mft_no] = (e.name, e.parent_ref[0], e.parent_ref[1], e.seq, e.is_dir, in_use)
            if not in_use and not e.is_dir:
                deleted.append(e)
        for e in deleted:
            e.path = self.build_path(e.parent_ref)
        return deleted

    def build_path(self, parent_ref, depth=0):
        """Rebuild folder path by walking parent references. '?' = unknown link."""
        parts = []
        ref = parent_ref
        seen = set()
        while ref and depth < 64:
            no, seq = ref
            if no == 5:
                break  # root
            if no in seen:
                parts.append("?")
                break
            seen.add(no)
            info = self.names.get(no)
            if not info:
                parts.append("?")
                break
            name, p_no, p_seq, cur_seq, is_dir, in_use = info
            # the record was reused for something else if the sequence moved on
            if seq and cur_seq != seq and cur_seq != seq + 1:
                parts.append("?")
                break
            parts.append(name)
            ref = (p_no, p_seq)
            depth += 1
        parts.reverse()
        return "\\".join(parts)

    # ---- cluster bitmap (to detect overwritten data) ------------------
    def load_bitmap(self):
        """Read $Bitmap (MFT record 6) once; 1 bit per cluster, 1 = allocated."""
        if self.bitmap is not None:
            return
        try:
            raw = self.vol.read(self._record_offset(6), self.record_size)
            rec = self._fixup(raw)
            for a in self._attributes(rec):
                if a["type"] == ATTR_DATA and a["name"] == "":
                    if a["resident"]:
                        self.bitmap = a["data"]
                    else:
                        self.bitmap = self.read_runs(a["runs"], a["size"])
                    return
        except Exception:
            pass
        self.bitmap = b""

    def _record_offset(self, mft_no):
        rs = self.record_size
        cs = self.cluster_size
        target = mft_no * rs
        vcn_bytes = 0
        for run in self.mft_runs:
            run_bytes = run.length * cs
            if vcn_bytes + run_bytes > target:
                if run.lcn is None:
                    raise ValueError("sparse MFT run")
                return run.lcn * cs + (target - vcn_bytes)
            vcn_bytes += run_bytes
        raise ValueError("MFT record %d beyond MFT" % mft_no)

    def cluster_allocated(self, lcn):
        if not self.bitmap:
            return None
        byte = lcn >> 3
        if byte >= len(self.bitmap):
            return None
        return bool(self.bitmap[byte] & (1 << (lcn & 7)))

    def check_overwritten(self, e):
        """Set e.status to OK / PARTIAL / OVERWRITTEN based on cluster bitmap."""
        if e.resident_data is not None or not e.runs:
            return
        self.load_bitmap()
        if not self.bitmap:
            return
        total = used = 0
        for run in e.runs:
            if run.lcn is None:
                continue
            # sample up to 64 clusters per run to keep it quick on huge files
            step = max(1, run.length // 64)
            for i in range(0, run.length, step):
                st = self.cluster_allocated(run.lcn + i)
                if st is None:
                    continue
                total += 1
                used += 1 if st else 0
        if total == 0:
            return
        if used == 0:
            e.status = "OK"
        elif used == total:
            e.status = "OVERWRITTEN"
        else:
            e.status = "PARTIAL"

    # ---- extraction ---------------------------------------------------
    def extract(self, e, dest_path, progress=None):
        """Write the deleted file's data to dest_path (outside the volume)."""
        if e.resident_data is not None:
            with open(dest_path, "wb") as f:
                f.write(e.resident_data[:e.size] if e.size else e.resident_data)
            return len(e.resident_data)
        written = [0]
        with open(dest_path, "wb") as f:
            def cb(chunk):
                f.write(chunk)
                written[0] += len(chunk)
                if progress:
                    progress(written[0], e.size)
            self.read_runs(e.runs, e.size, chunk_cb=cb)
        return written[0]

    def read_head(self, e, n):
        if e.resident_data is not None:
            return e.resident_data[:n]
        return self.read_runs(e.runs, e.size, max_bytes=n)


# --------------------------------------------------------------------------
#  Signature-based carving
# --------------------------------------------------------------------------

Sig = namedtuple("Sig", "ext magic offset max_size finder")

ZIP_EOCD = b"PK\x05\x06"


def _end_jpeg(buf, start, limit):
    i = buf.find(b"\xff\xd9", start + 2, limit)
    return i + 2 if i != -1 else None


def _end_png(buf, start, limit):
    i = buf.find(b"IEND", start + 8, limit)
    return i + 8 if i != -1 else None


def _end_gif(buf, start, limit):
    i = buf.find(b"\x00\x3b", start + 13, limit)
    return i + 2 if i != -1 else None


def _end_pdf(buf, start, limit):
    i = buf.rfind(b"%%EOF", start, limit)
    if i == -1:
        return None
    j = i + 5
    while j < limit and buf[j:j + 1] in (b"\r", b"\n"):
        j += 1
    return j


def _end_zip(buf, start, limit):
    i = buf.find(ZIP_EOCD, start, limit)
    if i == -1:
        return None
    if i + 22 <= len(buf):
        clen = struct.unpack_from("<H", buf, i + 20)[0]
        return i + 22 + clen
    return i + 22


def _end_mp4(buf, start, limit):
    """Walk ISO-BMFF boxes from the ftyp box until data stops making sense."""
    pos = start
    while pos + 8 <= limit:
        size = struct.unpack_from(">I", buf, pos)[0]
        typ = buf[pos + 4:pos + 8]
        if size == 1 and pos + 16 <= limit:
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
        if size == 0:
            return limit
        if size < 8 or not re.fullmatch(rb"[A-Za-z0-9 ]{4}", typ):
            break
        pos += size
    return pos if pos > start + 8 else None


def _end_7z(buf, start, limit):
    if start + 32 > len(buf):
        return None
    next_off, next_size = struct.unpack_from("<QQ", buf, start + 12)
    end = start + 32 + next_off + next_size
    return end if 0 < next_off + next_size < (limit - start) else None


SIGNATURES = [
    Sig("jpg", b"\xff\xd8\xff", 0, 40 << 20, _end_jpeg),
    Sig("png", b"\x89PNG\r\n\x1a\n", 0, 60 << 20, _end_png),
    Sig("gif", b"GIF8", 0, 30 << 20, _end_gif),
    Sig("pdf", b"%PDF-", 0, 200 << 20, _end_pdf),
    Sig("zip", b"PK\x03\x04", 0, 300 << 20, _end_zip),   # also docx/xlsx/pptx
    Sig("mp4", b"ftyp", 4, 2 << 30, _end_mp4),
    Sig("7z", b"7z\xbc\xaf\x27\x1c", 0, 1 << 30, _end_7z),
    Sig("rar", b"Rar!\x1a\x07", 0, 1 << 30, None),
    Sig("mp3", b"ID3", 0, 30 << 20, None),
]

OFFICE_HINT = {b"word/": "docx", b"xl/": "xlsx", b"ppt/": "pptx"}


class Carver:
    CHUNK = 16 << 20
    OVERLAP = 64 << 10

    def __init__(self, vol, start=0, end=None, min_size=1024, max_total=None, allocated_filter=None):
        self.vol = vol
        self.start = start
        self.end = end if end is not None else vol.size
        self.min_size = min_size
        self.allocated_filter = allocated_filter  # callable(byte_offset) -> True if in use (skip)
        self.max_total = max_total

    def scan(self, progress=None):
        found = []
        pos = self.start
        last_end = 0
        magics = [(s, s.magic) for s in SIGNATURES]
        while pos < self.end:
            n = min(self.CHUNK, self.end - pos)
            buf = self.vol.read(pos, n)
            if not buf:
                break
            search_end = len(buf) - (self.OVERLAP if pos + n < self.end else 0)
            for sig, magic in magics:
                i = buf.find(magic, 0, search_end)
                while i != -1:
                    hdr_start = i - sig.offset
                    abs_off = pos + hdr_start
                    if hdr_start >= 0 and abs_off >= last_end and self._plausible(sig, buf, hdr_start):
                        if not (self.allocated_filter and self.allocated_filter(abs_off)):
                            e = self._carve_at(sig, abs_off)
                            if e:
                                found.append(e)
                                last_end = abs_off + e.size
                                if self.max_total and len(found) >= self.max_total:
                                    return found
                    i = buf.find(magic, i + 1, search_end)
            pos += search_end if search_end > 0 else n
            if progress:
                progress(pos - self.start, self.end - self.start, len(found))
        return found

    @staticmethod
    def _plausible(sig, buf, i):
        if sig.ext == "jpg":
            return buf[i + 3:i + 4] in (b"\xe0", b"\xe1", b"\xe2", b"\xdb", b"\xc4", b"\xe8", b"\xfe", b"\xee")
        if sig.ext == "mp4":
            size = struct.unpack_from(">I", buf, i)[0] if i + 4 <= len(buf) else 0
            return 8 <= size <= 4096
        if sig.ext == "mp3":
            return i + 10 <= len(buf) and buf[i + 3] < 5
        if sig.ext == "zip":
            return i + 30 <= len(buf) and struct.unpack_from("<H", buf, i + 4)[0] <= 63
        return True

    def _carve_at(self, sig, abs_off):
        window = min(sig.max_size, self.end - abs_off)
        data = self.vol.read(abs_off, min(window, 4 << 20))
        end = sig.finder(data, 0, len(data)) if sig.finder else None
        if end is None and sig.finder and window > len(data):
            # footer not in first 4 MB: read the whole allowed window
            data = self.vol.read(abs_off, window)
            end = sig.finder(data, 0, len(data))
        if end is None:
            if sig.finder:
                return None  # no footer = too unreliable, skip
            end = min(window, 8 << 20)  # headerless-footer types: fixed slab
        size = end
        if size < self.min_size:
            return None
        e = Entry()
        e.source = "CARVE"
        e.carve_offset = abs_off
        e.size = size
        e.ext = sig.ext
        if sig.ext == "zip":
            head = data[:min(len(data), 64 << 10)]
            for key, ext in OFFICE_HINT.items():
                if key in head:
                    e.ext = ext
                    break
        e.name = "carved_%010d.%s" % (abs_off // 512, e.ext)
        e.path = "(carved from free space @ sector %d)" % (abs_off // 512)
        e.status = "CARVED"
        return e

    def extract(self, e, dest_path, progress=None):
        written = 0
        with open(dest_path, "wb") as f:
            pos = e.carve_offset
            end = pos + e.size
            while pos < end:
                chunk = self.vol.read(pos, min(8 << 20, end - pos))
                if not chunk:
                    break
                f.write(chunk)
                pos += len(chunk)
                written += len(chunk)
                if progress:
                    progress(written, e.size)
        return written


# --------------------------------------------------------------------------
#  Interactive session
# --------------------------------------------------------------------------

IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}

HELP = """
Commands
  1,4,7-12   restore those index numbers       all     restore every listed file
  f jpg      filter by type or name text       f       clear the filter
  f VID- download   filter AND restore every match in one go
  download   restore everything currently listed (same as 'all')
  p 12       preview one file (opens it)       g       HTML gallery of all images
  s          show the table again              q       quit
"""


class Session:
    def __init__(self, vol, ntfs, out_dir, drive_label):
        self.vol = vol
        self.ntfs = ntfs
        self.out_dir = out_dir
        self.drive_label = drive_label
        self.entries = []      # all findings
        self.view = []         # currently displayed subset (index -> entry)
        self.filter_text = ""
        self.carver = Carver(vol)
        self.gallery_dir = os.path.join(out_dir, "_gallery")
        self.preview_dir = os.path.join(out_dir, "_preview")

    # ---- table ---------------------------------------------------------
    def apply_filter(self):
        t = self.filter_text.lower()
        if not t:
            self.view = list(self.entries)
        else:
            self.view = [e for e in self.entries
                         if t in e.name.lower() or t == e.ext or t in e.path.lower()
                         or t == e.status.lower() or t == e.source.lower()]

    def show_table(self, limit=None):
        rows = self.view
        if not rows:
            print("\n  (no entries match)\n")
            return
        name_w = min(48, max(12, max(len(e.name) for e in rows)))
        hdr = "%5s | %-*s | %-16s | %-5s | %9s | %-11s | %s" % (
            "#", name_w, "File name", "Approx. deleted", "Type", "Size", "Status", "Original folder")
        print()
        print(hdr)
        print("-" * min(len(hdr) + 30, 150))
        shown = 0
        for i, e in enumerate(rows, 1):
            if limit and shown >= limit:
                print("  ... %d more (use 'f <text>' to narrow, or 's all' to show everything)" % (len(rows) - shown))
                break
            nm = e.name if len(e.name) <= name_w else e.name[:name_w - 1] + "…"
            when = fmt_dt(e.mft_changed or e.modified) if e.source == "MFT" else "-"
            print("%5d | %-*s | %-16s | %-5s | %9s | %-11s | %s" % (
                i, name_w, nm, when, (e.ext or "?")[:5], human(e.size), e.status, e.path or "\\"))
            shown += 1
        print()
        print("  %d entries listed%s.   Output folder: %s" % (
            len(rows), (" (filter: '%s')" % self.filter_text) if self.filter_text else "", self.out_dir))
        counts = {}
        for e in self.entries:
            counts[e.status] = counts.get(e.status, 0) + 1
        print("  Status summary: " + ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
        print("  OK = data clusters still free (good chance)   PARTIAL/OVERWRITTEN = re-used by newer files")

    # ---- restoring -----------------------------------------------------
    def dest_for(self, e):
        if e.source == "MFT":
            folder = os.path.join(self.out_dir, *[safe_filename(p, "_") for p in e.path.split("\\") if p])
        else:
            folder = os.path.join(self.out_dir, "_carved")
        os.makedirs(folder, exist_ok=True)
        return unique_path(os.path.join(folder, safe_filename(e.name)))

    def restore_one(self, e, dest):
        t0 = time.time()
        last = [t0]
        printed = [False]

        def prog(done, total):
            if time.time() - last[0] > 0.5 and total:
                last[0] = time.time()
                printed[0] = True
                sys.stdout.write("\r      %s / %s" % (human(done), human(total)))
                sys.stdout.flush()
        if e.source == "MFT":
            n = self.ntfs.extract(e, dest, prog)
        else:
            n = self.carver.extract(e, dest, prog)
        if printed[0]:
            sys.stdout.write("\r" + " " * 40 + "\r")
        return n

    def restore(self, idxs):
        print("\nRestoring %d file(s) to %s" % (len(idxs), self.out_dir))
        ok = 0
        for i in idxs:
            e = self.view[i - 1]
            dest = self.dest_for(e)
            try:
                n = self.restore_one(e, dest)
                flag = "" if e.status in ("OK", "CARVED") else "   [%s - may be damaged]" % e.status
                print("  [%d] %-40s -> %s  (%s)%s" % (i, e.name[:40], dest, human(n), flag))
                ok += 1
            except Exception as ex:
                print("  [%d] %-40s FAILED: %s" % (i, e.name[:40], ex))
        print("\nDone: %d of %d restored. Files are in:\n  %s\n" % (ok, len(idxs), self.out_dir))

    # ---- preview / gallery --------------------------------------------
    def preview(self, i):
        e = self.view[i - 1]
        os.makedirs(self.preview_dir, exist_ok=True)
        dest = unique_path(os.path.join(self.preview_dir, safe_filename(e.name)))
        n = self.restore_one(e, dest)
        print("  Preview copy written: %s (%s)" % (dest, human(n)))
        if e.ext in IMAGE_EXT or e.ext in ("pdf", "txt", "docx", "xlsx", "pptx", "mp4", "mp3"):
            open_with_default_app(dest)
        else:
            head = self.ntfs.read_head(e, 256) if e.source == "MFT" else self.vol.read(e.carve_offset, 256)
            print("  First bytes: %r" % head[:96])

    def gallery(self, max_each=25 << 20, per_page=200):
        imgs = [(i, e) for i, e in enumerate(self.view, 1) if e.ext in IMAGE_EXT and 0 < e.size <= max_each]
        if not imgs:
            print("  No image files (jpg/png/gif/bmp/webp) in the current list.")
            return
        os.makedirs(self.gallery_dir, exist_ok=True)
        total = sum(e.size for _, e in imgs)
        print("  Building gallery for %d images (%s will be copied into %s)" % (len(imgs), human(total), self.gallery_dir))
        pages = [imgs[k:k + per_page] for k in range(0, len(imgs), per_page)]
        page_files = []
        for pno, page in enumerate(pages, 1):
            cards = []
            for i, e in page:
                fname = "%05d_%s" % (i, safe_filename(e.name))
                dest = os.path.join(self.gallery_dir, fname)
                try:
                    self.restore_one(e, dest)
                except Exception as ex:
                    print("   skip [%d] %s: %s" % (i, e.name, ex))
                    continue
                cards.append(
                    '<figure><a href="%s" target="_blank"><img loading="lazy" src="%s" alt=""></a>'
                    '<figcaption><b>#%d</b> %s<br><small>%s · %s · %s<br>%s</small></figcaption></figure>' % (
                        html.escape(fname), html.escape(fname), i, html.escape(e.name), human(e.size),
                        e.status, html.escape(fmt_dt(e.mft_changed or e.modified)), html.escape(e.path or "\\")))
            nav = " ".join('<a href="gallery_%d.html">page %d</a>' % (k, k) for k in range(1, len(pages) + 1))
            doc = ("<!doctype html><meta charset='utf-8'><title>Recovered images - page %d</title>"
                   "<style>body{font-family:Segoe UI,Arial,sans-serif;background:#111;color:#eee;margin:16px}"
                   ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}"
                   "figure{margin:0;background:#1d1d1d;border-radius:8px;padding:8px}"
                   "img{width:100%%;height:180px;object-fit:contain;background:#000;border-radius:4px}"
                   "figcaption{font-size:12px;margin-top:6px;word-break:break-all}small{color:#aaa}"
                   "a{color:#8cf}</style>"
                   "<h2>%s - deleted images (page %d of %d)</h2><p>%s</p>"
                   "<p>Note the <b>#index</b> of the images you want, then type those numbers in the tool "
                   "(e.g. <code>3,7,10-14</code>). Broken thumbnails = data partly overwritten.</p>"
                   "<div class='grid'>%s</div>") % (pno, html.escape(self.drive_label), pno, len(pages), nav, "".join(cards))
            pf = os.path.join(self.gallery_dir, "gallery_%d.html" % pno)
            with open(pf, "w", encoding="utf-8") as f:
                f.write(doc)
            page_files.append(pf)
        print("  Gallery ready: %s" % page_files[0])
        open_with_default_app(page_files[0])

    # ---- loop ----------------------------------------------------------
    def loop(self):
        self.apply_filter()
        self.show_table(limit=400)
        print(HELP)
        while True:
            try:
                cmd = input("recover> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not cmd:
                continue
            low = cmd.lower()
            if low in ("q", "quit", "exit"):
                return
            if low == "s":
                self.show_table(limit=400)
                continue
            if low == "s all":
                self.show_table()
                continue
            if low in ("h", "help", "?"):
                print(HELP)
                continue
            if low == "f":
                self.filter_text = ""
                self.apply_filter()
                self.show_table(limit=400)
                continue
            if low.startswith("f "):
                text = cmd[2:].strip()
                auto_restore = False
                m = re.fullmatch(r"(.*?)\s+(download|restore|d)", text, re.IGNORECASE)
                if m:
                    text, auto_restore = m.group(1).strip(), True
                self.filter_text = text.lstrip(".")
                self.apply_filter()
                self.show_table(limit=400)
                if auto_restore:
                    if not self.view:
                        continue
                    low = "all"      # fall through to the restore path below
                else:
                    continue
            if low == "g":
                self.gallery()
                continue
            if low.startswith("p "):
                try:
                    i = parse_selection(cmd[2:], len(self.view))[0]
                    self.preview(i)
                except ValueError as ex:
                    print("  ", ex)
                continue
            if low in ("all", "download", "d"):
                if not self.view:
                    print("  Nothing listed to restore.")
                    continue
                idxs = list(range(1, len(self.view) + 1))
            else:
                try:
                    idxs = parse_selection(cmd, len(self.view))
                except ValueError as ex:
                    print("  ", ex, "- type 'h' for help")
                    continue
            total = sum(self.view[i - 1].size for i in idxs)
            ans = input("  Restore %d file(s), %s total, into %s ? [y/N] " % (len(idxs), human(total), self.out_dir)).strip().lower()
            if ans in ("y", "yes"):
                self.restore(idxs)


def open_with_default_app(path):
    try:
        if IS_WINDOWS:
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        print("  (could not auto-open: %s)" % ex)


# --------------------------------------------------------------------------
#  Main flow
# --------------------------------------------------------------------------

def pause(msg="Press Enter to exit."):
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


def ask_drive():
    while True:
        s = input("Drive letter of the partition to scan (e.g. D or D:): ").strip().rstrip("\\/")
        if re.fullmatch(r"[A-Za-z]:?", s):
            return s[0].upper()
        print("  Please enter a single drive letter.")


def ask_yes(prompt, default=False):
    s = input("%s [%s] " % (prompt, "Y/n" if default else "y/N")).strip().lower()
    if not s:
        return default
    return s in ("y", "yes")


def main():
    ap = argparse.ArgumentParser(
        prog="ntfs_recover",
        description="Read-only undelete for NTFS volumes. Source drive is never written to.",
        epilog="Project page: " + __url__)
    ap.add_argument("--version", action="version", version="Deleted-Files-Recovery " + __version__)
    ap.add_argument("--image", help="scan a raw image file instead of a live drive")
    ap.add_argument("--out", help="output folder (default: your Downloads folder)")
    ap.add_argument("--no-carve", action="store_true", help="skip the signature scan")
    ap.add_argument("--carve", action="store_true", help="run the signature scan without asking")
    ap.add_argument("--drive", help="drive letter (skips the prompt)")
    ap.add_argument("--min-size", type=int, default=1024, help="ignore carved files smaller than this (bytes, default 1024)")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # never crash on exotic file names in old consoles
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    print("=" * 78)
    print(" Deleted-Files-Recovery %s   (%s)" % (VERSION, __url__))
    print(" Source drive is opened read-only. Recovered files go to your Downloads folder.")
    print("=" * 78)

    # ----- elevation --------------------------------------------------
    if IS_WINDOWS and not args.image and not is_admin():
        print("\nAdministrator rights are needed for raw disk access. Requesting elevation...")
        if relaunch_as_admin():
            sys.exit(0)
        print("Could not elevate automatically. Right-click Terminal/CMD -> 'Run as administrator'\n"
              "and run:  python \"%s\"" % os.path.abspath(sys.argv[0]))
        pause()
        sys.exit(1)

    # ----- source -----------------------------------------------------
    if args.image:
        src_path, is_dev, label = args.image, False, os.path.basename(args.image)
        drive = None
    else:
        if not IS_WINDOWS:
            print("Live drive scanning works on Windows only. Use --image <file> elsewhere.")
            sys.exit(1)
        drive = (args.drive or ask_drive())[0].upper()
        src_path, is_dev, label = r"\\.\%s:" % drive, True, "%s:" % drive

    # ----- output folder, with the safety guard ----------------------
    base_out = os.path.abspath(args.out) if args.out else downloads_folder()
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = os.path.join(base_out, "Recovered_%s_%s" % ((drive or safe_filename(label)), stamp))
    import ntpath
    if drive and ntpath.splitdrive(base_out)[0].upper().rstrip("\\") == drive + ":":
        print("\nSTOP: the output folder (%s) is on the same drive you want to scan (%s:)." % (base_out, drive))
        print("Writing there could overwrite the very data you are trying to recover.")
        print("Use --out <folder on another drive> or scan a different drive.")
        pause()
        sys.exit(2)

    # ----- open -------------------------------------------------------
    try:
        vol = RawVolume(src_path, is_dev)
    except PermissionError:
        print("\nAccess denied opening %s. Run as Administrator." % src_path)
        pause()
        sys.exit(1)
    except OSError as ex:
        print("\nCannot open %s: %s" % (src_path, ex))
        pause()
        sys.exit(1)

    ntfs = None
    try:
        ntfs = NTFS(vol)
        print("\nSource: %s   NTFS, cluster %d B, MFT record %d B, %s, %d MFT records" % (
            label, ntfs.cluster_size, ntfs.record_size, human(ntfs.volume_size), ntfs.record_count()))
    except ValueError as ex:
        print("\nSource: %s - %s" % (label, ex))
        print("Metadata recovery needs NTFS. Only signature carving is available on this volume.")
        if vol.size is None:
            print("Cannot determine the volume size; aborting.")
            sys.exit(1)

    entries = []
    # ----- MFT scan ---------------------------------------------------
    if ntfs:
        print("Scanning the $MFT for deleted entries...")
        t0 = time.time()

        def prog(done, total):
            sys.stdout.write("\r   %d / %d records" % (done, total))
            sys.stdout.flush()
        try:
            deleted = ntfs.scan(prog)
        finally:
            sys.stdout.write("\r" + " " * 40 + "\r")
        print("   %d deleted file entries with intact names (%.1fs)." % (len(deleted), time.time() - t0))
        print("   Checking which ones still have their data clusters free...")
        for e in deleted:
            ntfs.check_overwritten(e)
        deleted.sort(key=lambda e: (e.path.lower(), e.name.lower()))
        entries.extend(deleted)

    # ----- carving ----------------------------------------------------
    do_carve = False
    if not args.no_carve:
        if args.carve:
            do_carve = True
        else:
            print("\nSignature carving reads the WHOLE partition (%s) sequentially." % human(vol.size or 0))
            print("On a mechanical HDD expect roughly 1 hour per 400-500 GB. It finds files whose MFT")
            print("record is already gone, but without their names or dates.")
            do_carve = ask_yes("Run the signature scan now?", default=False)
    if do_carve:
        if ntfs:
            ntfs.load_bitmap()
            cs = ntfs.cluster_size

            def allocated(off):
                return ntfs.cluster_allocated(off // cs) is True
            carver = Carver(vol, min_size=args.min_size, allocated_filter=allocated)
        else:
            carver = Carver(vol, min_size=args.min_size)
        t0 = time.time()
        last = [0.0]

        def cprog(done, total, n):
            if time.time() - last[0] > 0.5 or done >= total:
                last[0] = time.time()
                pct = 100.0 * done / total if total else 0
                el = time.time() - t0
                eta = (el / done * (total - done)) if done else 0
                sys.stdout.write("\r   carving %5.1f%%  %s found  elapsed %dm%02ds  ETA %dm%02ds   " % (
                    pct, n, el // 60, el % 60, eta // 60, eta % 60))
                sys.stdout.flush()
        try:
            carved = carver.scan(cprog)
        except KeyboardInterrupt:
            carved = []
            print("\n   carving interrupted")
        if ntfs and ntfs.bitmap:
            print("\n   %d carved files (free space only; clusters used by live files were skipped)." % len(carved))
        else:
            print("\n   %d carved files (no cluster map on this volume, so live files are included too)." % len(carved))
        entries.extend(carved)

    if not entries:
        if ntfs is None and not do_carve:
            print("\nNot an NTFS volume and the signature scan was skipped, so there is nothing to list.")
        else:
            print("\nNothing recoverable was found.")
        vol.close()
        return

    os.makedirs(out_dir, exist_ok=True)
    sess = Session(vol, ntfs, out_dir, label)
    sess.entries = entries
    if do_carve:
        sess.carver = carver
    try:
        sess.loop()
    finally:
        vol.close()
    print("Finished. Source drive was never written to. Output folder: %s" % out_dir)
    if IS_WINDOWS and not args.image:
        pause("Press Enter to close.")


if __name__ == "__main__":
    main()
