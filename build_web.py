"""Assemble light_bridge/web/ from web-src/ (single self-contained index.html)."""
import json, pathlib
root = pathlib.Path(__file__).parent
src, out = root / "web-src", root / "light_bridge" / "web"
out.mkdir(parents=True, exist_ok=True)
html = (src / "index.html").read_text()
html = html.replace("/*__QR_JS__*/", (src / "qr.js").read_text()).replace("/*__APP_JS__*/", (src / "app.js").read_text())
(out / "index.html").write_text(html)
(out / "manifest.webmanifest").write_text(json.dumps({
    "name": "Light Bridge", "short_name": "Lights", "scope": "/", "display": "standalone",
    "background_color": "#0b0f10", "theme_color": "#0b0f10",
    "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]}, indent=1))
print("index.html", len(html), "bytes")
