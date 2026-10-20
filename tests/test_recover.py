"""End-to-end tests for ntfs_recover.py.

Run from the repo root:

    tests/make_image.sh /tmp/ntfs-test        # needs ntfs-3g, once
    python3 -m pytest tests/ -v

or without pytest:

    python3 tests/test_recover.py /tmp/ntfs-test

The image builder needs ntfs-3g (Linux). The tests themselves only need the
image and manifest it produces, so they run anywhere.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "ntfs_recover.py")
sys.path.insert(0, ROOT)

import ntfs_recover as nr  # noqa: E402

IMAGE_DIR = os.environ.get("NTFS_TEST_DIR") or (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "/tmp/ntfs-test")
IMAGE = os.path.join(IMAGE_DIR, "test.img")
MANIFEST = os.path.join(IMAGE_DIR, "manifest.json")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_tool(stdin, *extra):
    """Run the CLI against the test image with scripted input; return (stdout, out_dir)."""
    out = tempfile.mkdtemp(prefix="recovery-test-out-")
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--image", IMAGE, "--out", out, *extra],
        input=stdin, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    subdirs = [d for d in os.listdir(out) if d.startswith("Recovered_")]
    return proc.stdout, (os.path.join(out, subdirs[0]) if subdirs else None)


class UnitTests(unittest.TestCase):
    """Pure-Python pieces, no image needed."""

    def test_selection_parser(self):
        self.assertEqual(nr.parse_selection("1,3,5-7", 10), [1, 3, 5, 6, 7])
        self.assertEqual(nr.parse_selection(" 7-5 ", 10), [5, 6, 7])
        self.assertEqual(nr.parse_selection("2,2,2", 3), [2])
        for bad in ["0", "11", "a", "1-", "-3", "", ","]:
            with self.assertRaises(ValueError, msg=bad):
                nr.parse_selection(bad, 10)

    def test_safe_filename(self):
        self.assertEqual(nr.safe_filename('a<b>:c?.txt'), "a_b__c_.txt")
        self.assertEqual(nr.safe_filename("  ...  "), "unnamed")
        self.assertEqual(nr.safe_filename("Façade été.png"), "Façade été.png")

    def test_human_sizes(self):
        self.assertEqual(nr.human(0), "0 B")
        self.assertEqual(nr.human(1024), "1.0 KB")
        self.assertEqual(nr.human(5 * 2 ** 20), "5.0 MB")

    def test_data_run_decoding(self):
        # 0x21 = 1 length byte, 2 offset bytes; length 8 at LCN 0x1234, then end.
        runs = nr.NTFS.decode_runs(b"\x21\x08\x34\x12\x00", 0, 5)
        self.assertEqual(runs, [nr.DataRun(0x1234, 8)])
        # sparse run (offset size 0)
        runs = nr.NTFS.decode_runs(b"\x01\x05\x00", 0, 3)
        self.assertEqual(runs, [nr.DataRun(None, 5)])

    def test_filetime(self):
        self.assertIsNone(nr.filetime_to_dt(0))
        d = nr.filetime_to_dt(132539760000000000)  # 2021-01-01 12:00 UTC
        self.assertEqual((d.year, d.month), (2021, 1))


@unittest.skipUnless(os.path.exists(IMAGE) and os.path.exists(MANIFEST),
                     "test image missing; run tests/make_image.sh first")
class ImageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(MANIFEST, encoding="utf-8") as f:
            cls.manifest = json.load(f)
        cls.deleted = {k: v for k, v in cls.manifest.items() if v["deleted"]}
        cls.live = {k: v for k, v in cls.manifest.items() if not v["deleted"]}

    def test_mft_scan_finds_every_deleted_name(self):
        vol = nr.RawVolume(IMAGE, False)
        fs = nr.NTFS(vol)
        entries = fs.scan()
        vol.close()
        found = {e.full_path.replace("\\", "/") for e in entries}
        for rel in self.deleted:
            self.assertIn(rel, found, "deleted file not listed: " + rel)
        for rel in self.live:
            self.assertNotIn(rel, found, "live file wrongly listed as deleted: " + rel)

    def test_restore_all_is_byte_exact_for_ok_entries(self):
        stdout, out_dir = run_tool("all\ny\nq\n", "--no-carve")
        self.assertIsNotNone(out_dir)
        ok_names = set()
        for line in stdout.splitlines():
            cells = [c.strip() for c in line.split("|")]
            if len(cells) >= 7 and cells[5] == "OK":
                ok_names.add(cells[1])
        self.assertGreater(len(ok_names), 5)
        checked = 0
        for rel, info in self.deleted.items():
            path = os.path.join(out_dir, *rel.split("/"))
            self.assertTrue(os.path.exists(path), "not restored: " + rel)
            if os.path.basename(rel) in ok_names:
                self.assertEqual(sha256(path), info["sha256"], "content differs: " + rel)
                self.assertEqual(os.path.getsize(path), info["size"])
                checked += 1
        self.assertGreater(checked, 5)

    def test_filter_and_download(self):
        stdout, out_dir = run_tool("f VID- download\ny\nq\n", "--no-carve")
        restored = [os.path.join(dp, f) for dp, _, fs in os.walk(out_dir) for f in fs]
        self.assertEqual(len(restored), 1)
        self.assertTrue(restored[0].endswith("VID-20240101.mp4"))

    def test_filter_with_no_match_restores_nothing(self):
        stdout, out_dir = run_tool("f nothing-matches-this download\nq\n", "--no-carve")
        self.assertIn("no entries match", stdout)
        files = [f for _, _, fs in os.walk(out_dir) for f in fs]
        self.assertEqual(files, [])

    def test_bad_selections_are_rejected(self):
        stdout, _ = run_tool("0\n999\nabc\nq\n", "--no-carve")
        self.assertIn("outside 1-", stdout)
        self.assertIn("Cannot understand", stdout)

    def test_carving_returns_exact_copies(self):
        stdout, out_dir = run_tool("f carve\nall\ny\nq\n", "--carve")
        carved_dir = os.path.join(out_dir, "_carved")
        self.assertTrue(os.path.isdir(carved_dir))
        known = {v["sha256"] for v in self.manifest.values()}
        hits = [f for f in os.listdir(carved_dir) if sha256(os.path.join(carved_dir, f)) in known]
        self.assertGreaterEqual(len(hits), 3, "expected carved jpg/png/pdf to match originals")

    def test_gallery_is_generated(self):
        stdout, out_dir = run_tool("g\nq\n", "--no-carve")
        page = os.path.join(out_dir, "_gallery", "gallery_1.html")
        self.assertTrue(os.path.exists(page))
        with open(page, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("<figure>", html)
        self.assertIn("IMG_0001.jpg", html)

    def test_source_image_is_never_modified(self):
        before = sha256(IMAGE)
        run_tool("all\ny\ng\nq\n", "--carve")
        self.assertEqual(sha256(IMAGE), before, "the tool wrote to the source volume")


class NonNtfsTests(unittest.TestCase):

    def test_garbage_input_exits_cleanly(self):
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            f.write(os.urandom(1 << 20))
            junk = f.name
        try:
            out = tempfile.mkdtemp()
            proc = subprocess.run([sys.executable, SCRIPT, "--image", junk, "--out", out, "--no-carve"],
                                  input="q\n", capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Not an NTFS volume", proc.stdout)
        finally:
            os.unlink(junk)

    def test_missing_image_reports_error(self):
        proc = subprocess.run([sys.executable, SCRIPT, "--image", "/nonexistent/x.img", "--out", tempfile.mkdtemp()],
                              input="", capture_output=True, text=True, timeout=60)
        self.assertIn("Cannot open", proc.stdout)


if __name__ == "__main__":
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a.startswith("-")]
    unittest.main(verbosity=2)
