# LXGW WenKai Screen

This directory bundles the complete LXGW WenKai Screen v1.522 font for local HTML poster rendering.
No glyphs are removed and no glyph designs are changed. The WOFF2 file is used only as an embedded web font.

- Project: https://github.com/lxgw/LxgwWenKai-Screen
- Original font: https://github.com/lxgw/LxgwWenKai-Screen/releases/download/v1.522/LXGWWenKaiScreen.ttf
- License: OFL.txt, SIL Open Font License 1.1 with the upstream web-format conversion permission.
- Original TTF SHA-256: `cd1a6fa39c4ea42fd8f4e289945789b0e510cf7016435640f8893cdad9b220f3`
- Bundled WOFF2 SHA-256: `30de45b002f8cd07b77337d10d5ae2d8c3172c1b2a4a0096f1a5637023308b41`

The WOFF2 is converted once during development with FontTools and Brotli:

```python
from fontTools.ttLib import TTFont

font = TTFont("LXGWWenKaiScreen.ttf")
font.flavor = "woff2"
font.save("LXGWWenKaiScreen.woff2")
```

FontTools and Brotli are not runtime dependencies. Posters embed the bundled font as a data URL,
so rendering requires neither system font installation nor external font requests.
