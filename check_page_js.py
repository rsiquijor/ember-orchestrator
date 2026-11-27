#!/usr/bin/env python3
"""Extract inline <script> blocks from demo_page.html and node --check them."""
import re
import subprocess
import sys
import tempfile
import os

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_page.html")
with open(path) as f:
    html = f.read()

blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
print(f"found {len(blocks)} script block(s)")
ok = True
for i, js in enumerate(blocks):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as t:
        t.write(js)
        name = t.name
    r = subprocess.run(["node", "--check", name], capture_output=True, text=True)
    os.unlink(name)
    if r.returncode != 0:
        ok = False
        print(f"block {i}: SYNTAX ERROR\n{r.stderr}")
    else:
        print(f"block {i}: OK ({len(js)} chars)")
sys.exit(0 if ok else 1)
