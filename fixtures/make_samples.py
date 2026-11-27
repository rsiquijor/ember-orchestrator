#!/usr/bin/env python3
"""Generate the multimodal intake test samples (Nov 26, 2026).

Deterministic ground-truth fixtures for the unified intake:
  sample-invoice.pdf  — INV-88410 with the planted $7.50 discrepancy
                        (same figures as sample-invoice.png)
  sample-workorder.wav — spoken work order (Cube TTS :8001, Kokoro
                        am_michael): WO 7712, hydraulic pump, line 3,
                        priority high, due Friday
  sample-fault.mp4    — 4s alert screen: "PUMP FAULT - LINE 3" /
                        "PRESSURE 42 PSI" (ffmpeg lavfi + drawtext)

Run on the Cube (needs TTS + ffmpeg):  python3 make_samples.py
"""
import json
import os
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def make_pdf(path):
    lines = [
        "Hydraulic Supply Co.",
        "INVOICE INV-88410",
        "Date: 11/18/2026    PO: PO-2214",
        "Bill to: Demo Plant - Accounts Payable",
        "",
        "Hydraulic hose, 3/4 in x 6 ft   x2 @ 86.50 = 173.00",
        "Hydraulic oil ISO 46, 55 gal    x1 @ 812.00 = 812.00",
        "Manifold fitting kit            x1 @ 257.50 = 257.50",
        "",
        "Subtotal: 1,242.50",
        "Total due: 1,250.00",
    ]
    content = "BT /F1 13 Tf 56 740 Td 18 TL "
    for i, ln in enumerate(lines):
        esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content += f"({esc}) Tj " + ("T* " if i < len(lines) - 1 else "")
    content += "ET"
    stream = content.encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(pdf)
    pdf += f"xref\n0 {len(objs) + 1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    with open(path, "wb") as f:
        f.write(pdf)
    return len(pdf)


def make_wav(path, tts_url="http://127.0.0.1:8001/tts"):
    text = ("Work order seven seven one two. Replace the hydraulic "
            "pump on line three. Priority high. Due Friday.")
    req = urllib.request.Request(
        tts_url, data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def make_mp4(path):
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    vf = (f"drawtext=fontfile={font}:text='PUMP FAULT - LINE 3':"
          "fontcolor=white:fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2-30,"
          f"drawtext=fontfile={font}:text='PRESSURE 42 PSI':"
          "fontcolor=yellow:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2+20")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "color=c=0x102030:s=640x360:d=4:r=24",
         "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", path],
        check=True, capture_output=True)
    return os.path.getsize(path)


if __name__ == "__main__":
    print("pdf:", make_pdf(os.path.join(HERE, "sample-invoice.pdf")), "bytes")
    print("wav:", make_wav(os.path.join(HERE, "sample-workorder.wav")), "bytes")
    print("mp4:", make_mp4(os.path.join(HERE, "sample-fault.mp4")), "bytes")
