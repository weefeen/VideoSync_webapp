"""Rasterise one engraved page plate for the drifting backdrop.

The live site fetches /api/library/<work>/page?n=1 and tiles it; this is the
same plate out of the same package, trimmed to its engraving the way
app/routes.py trims it before serving.
"""
import base64, io, pathlib, shutil, subprocess

PKG = pathlib.Path(r"C:\ZZ_perso\weefeen\PT\RD\music_line_extractor\project"
                   r"\Op.39_3ème Scherzo pour le Piano_(Breitkopf)__039-1-BH")
HERE = pathlib.Path(__file__).parent
TMP = HERE / "raster"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
W = 1100

from PIL import Image, ImageChops

src = PKG / "pages" / "page_001.svg"
flat = TMP / "plate.svg"
shutil.copyfile(src, flat)
png = TMP / "plate.png"

subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--force-device-scale-factor=1", "--no-sandbox",
                "--default-background-color=FFFFFFFF",
                f"--window-size={W},{round(W*2417/2051)}",
                f"--screenshot={png}", flat.as_uri()],
               check=True, capture_output=True, timeout=180)

im = Image.open(png).convert("L")
box = ImageChops.invert(im).getbbox()          # crop to the engraving
im = im.crop(box)
print("plate trimmed to", im.size)

buf = io.BytesIO()
im.save(buf, "WEBP", quality=74, method=5)
data = buf.getvalue()
(HERE / "plate.b64").write_text(
    "data:image/webp;base64," + base64.b64encode(data).decode(), encoding="utf-8")
print(f"plate: {len(data)/1024:.0f} KB")
