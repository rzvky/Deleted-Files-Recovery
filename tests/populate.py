"""Fill a mounted test volume with files, delete some, record what happened.

Used by make_image.sh. Writes a manifest so test_recover.py can check that
every restored file is byte-identical to what was deleted.
"""
import hashlib
import io
import json
import os
import random
import struct
import sys
import zipfile
import zlib

random.seed(1234)


def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def jpeg(n):
    # Not a decodable picture, but a structurally valid JPEG stream for carving tests.
    body = bytes(random.getrandbits(8) for _ in range(n)).replace(b"\xff", b"\xfe")
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + body + b"\xff\xd9"


def pdf(n):
    filler = os.urandom(n).replace(b"%%EOF", b"-----")
    return b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n% " + filler + b"\ntrailer<<>>\n%%EOF\n"


def docx(n):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document>" + "x" * n + "</w:document>")
    return buf.getvalue()


FILES = {
    # path: (data, delete?)
    "Photos/2024/IMG_0001.jpg": (jpeg(20_000), True),
    "Photos/2024/IMG_0002.jpg": (jpeg(150_000), True),
    "Photos/Screenshots/screen 1.png": (png(300, 200, (0, 0, 255)), True),
    "Photos/Screenshots/screen 2.png": (png(900, 700, (255, 200, 0)), True),
    "Docs/Report Q3.pdf": (pdf(300_000), True),
    "Docs/notes.txt": (b"resident file, lives inside the MFT record\n" * 3, True),
    "Docs/Thesis draft.docx": (docx(120_000), True),
    "Docs/tiny.txt": (b"tiny", True),
    "Video/VID-20240101.mp4": (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + os.urandom(2_000_000), True),
    "Big/large_random.bin": (os.urandom(6_000_000), True),
    "Unicode/Façade été फोटो.png": (png(50, 50, (9, 9, 9)), True),
    "Keep/alive.jpg": (jpeg(5_000), False),
    "Keep/alive.txt": (b"this one stays\n", False),
}


def main(mount, manifest_path):
    manifest = {}
    for rel, (data, delete) in FILES.items():
        p = os.path.join(mount, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        manifest[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "deleted": delete}
    os.sync()
    for rel, (_, delete) in FILES.items():
        if delete:
            os.remove(os.path.join(mount, *rel.split("/")))
    os.sync()
    # Write a new file afterwards so some freed clusters get re-used.
    # The test only asserts on entries the tool reports as OK.
    with open(os.path.join(mount, "Keep", "newer.bin"), "wb") as f:
        f.write(os.urandom(3_000_000))
    os.sync()
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    print("populated %d files, %d deleted" % (len(FILES), sum(1 for _, d in FILES.values() if d)))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
