import math
import os
from flask import Flask, Response, send_from_directory
import requests
from io import BytesIO
from PIL import Image
from render_fastighet import render_tile, find_gpkg_files

app = Flask(__name__)

# Lantmäteriet credentials — set LM_USER and LM_PASS in the container environment
AUTH = (os.environ["LM_USER"], os.environ["LM_PASS"])

_GPKG_FILES = find_gpkg_files()

TOPOWEBB_URL = (
    "https://maps.lantmateriet.se/open/topowebb-ccby/v1/wmts/1.0.0"
    "/topowebb/default/3857/{z}/{y}/{x}.png"
)

def _tile_to_bbox_3857(z, x, y):
    """Convert XYZ tile coords to EPSG:3857 bounding box."""
    n = 2 ** z

    def lon_to_m(lon):
        return lon * 20037508.34 / 180.0

    def lat_to_m(lat):
        lat_r = math.radians(lat)
        return math.log(math.tan(math.pi / 4 + lat_r / 2)) * 6378137.0

    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))

    return lon_to_m(lon_w), lat_to_m(lat_s), lon_to_m(lon_e), lat_to_m(lat_n)


_CORS = {"Access-Control-Allow-Origin": "*"}


@app.route("/<int:z>/<int:x>/<int:y>.png")
def tile(z, x, y):
    url = TOPOWEBB_URL.format(z=z, x=x, y=y)
    r = requests.get(url, auth=AUTH, timeout=10)
    return Response(r.content, content_type="image/png", status=r.status_code, headers=_CORS)


@app.route("/fastighet/<int:z>/<int:x>/<int:y>.png")
def fastighet(z, x, y):
    png = render_tile(z, x, y, _GPKG_FILES)
    return Response(png, content_type="image/png", headers=_CORS)


@app.route("/combined/<int:z>/<int:x>/<int:y>.png")
def combined(z, x, y):
    """Topowebb with fastighetsgränser baked in — usable as standalone base map."""
    url = TOPOWEBB_URL.format(z=z, x=x, y=y)
    r = requests.get(url, auth=AUTH, timeout=10)
    base = Image.open(BytesIO(r.content)).convert("RGBA")
    overlay = Image.open(BytesIO(render_tile(z, x, y, _GPKG_FILES))).convert("RGBA")
    base.paste(overlay, mask=overlay)
    buf = BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    return Response(buf.getvalue(), content_type="image/png", headers=_CORS)


@app.route("/draw")
def draw():
    return send_from_directory(".", "draw.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8085)
