#!/bin/sh
# Regenerate every logo asset from the SVG sources. Needs node + playwright and Python + Pillow.
set -e
cd "$(dirname "$0")"
python3 make_logo.py
STATIC=../syncalist/web/static
OUT=$(mktemp -d)
for n in 16 32; do node render.js logo-tiny.svg "$OUT/t$n.png" $n; done
node render.js logo-small.svg "$OUT/s48.png" 48
for n in 180 192 512; do node render.js logo.svg "$OUT/f$n.png" $n; done
cp logo-tiny.svg "$STATIC/favicon.svg"
cp logo.svg "$STATIC/logo.svg"
cp logo-small.svg "$STATIC/logo-small.svg"
cp "$OUT/t32.png" "$STATIC/favicon-32.png"
cp "$OUT/f180.png" "$STATIC/apple-touch-icon.png"
cp "$OUT/f192.png" "$STATIC/icon-192.png"
cp "$OUT/f512.png" "$STATIC/icon-512.png"
cp "$OUT/f512.png" ../unraid/icon.png
python3 - "$OUT" "$STATIC/favicon.ico" <<'PY'
import sys
from PIL import Image
out, ico = sys.argv[1], sys.argv[2]
imgs = [Image.open(f"{out}/{n}.png").convert("RGBA") for n in ("s48", "t32", "t16")]
imgs[0].save(ico, format="ICO", sizes=[(48, 48), (32, 32), (16, 16)], append_images=imgs[1:])
PY
node -e '
const { chromium } = require("playwright"); const path = require("path");
(async () => { const b = await chromium.launch(); const p = await b.newPage({viewport:{width:1280,height:640}});
  await p.goto("file://" + path.resolve("social.html")); await p.screenshot({path:"social-preview.png"}); await b.close(); })();'
rm -rf "$OUT"
echo "Assets regenerated."
