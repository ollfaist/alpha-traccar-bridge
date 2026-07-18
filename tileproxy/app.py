import math
import os
import base64
import json
import gzip
import sqlite3
from flask import Flask, Response, send_from_directory, request, jsonify
import requests
from io import BytesIO
from PIL import Image
from render_fastighet import (render_tile, find_gpkg_files, query_parcel,
                              _wgs84_to_sweref, _sweref_to_wgs84, _parse_gpkg_geom)

app = Flask(__name__)

# Lantmäteriet credentials — set LM_USER and LM_PASS in the container environment
AUTH = (os.environ["LM_USER"], os.environ["LM_PASS"])

_TRACCAR_URL  = os.environ.get("TRACCAR_URL",  "http://192.168.1.80:8190")
_TRACCAR_AUTH = (os.environ.get("TRACCAR_USER", ""), os.environ.get("TRACCAR_PASS", ""))

_GPKG_FILES = find_gpkg_files()

TOPOWEBB_URL = (
    "https://maps.lantmateriet.se/open/topowebb-ccby/v1/wmts/1.0.0"
    "/topowebb/default/3857/{z}/{y}/{x}.png"
)

GOOGLE_HYBRID_URL = "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"
_GOOGLE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tileproxy/1.0)"}

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


_LM_MAX_Z = 15
_LM_MIN_Z = 5

@app.route("/<int:z>/<int:x>/<int:y>.png")
def tile(z, x, y):
    # Clamp to LM native range and remap tile coords so we never request
    # non-existent tiles; Leaflet upscales/downscales the returned image.
    if z > _LM_MAX_Z:
        scale = 2 ** (z - _LM_MAX_Z)
        z, x, y = _LM_MAX_Z, x // scale, y // scale
    elif z < _LM_MIN_Z:
        scale = 2 ** (_LM_MIN_Z - z)
        z, x, y = _LM_MIN_Z, x * scale, y * scale
    url = TOPOWEBB_URL.format(z=z, x=x, y=y)
    r = requests.get(url, auth=AUTH, timeout=10)
    hdrs = dict(_CORS)
    if r.status_code == 200:
        hdrs["Cache-Control"] = "public, max-age=604800"
    return Response(r.content, content_type="image/png", status=r.status_code, headers=hdrs)


@app.route("/fastighet/<int:z>/<int:x>/<int:y>.png")
def fastighet(z, x, y):
    """Fullt fastighetslager: gränser + skraffering + beteckningar."""
    png = render_tile(z, x, y, _GPKG_FILES)
    return Response(png, content_type="image/png", headers=_CORS)


# Diskcache: renderade tiles sparas i /cache (Docker-volym) och överlever
# omstarter. Uppvärmningen körs därför bara första gången.
_CACHE_DIR = os.environ.get("TILE_CACHE_DIR", "/cache")

_VALID_STYLES = {"dashed", "solid", "lm"}

def _granser_png(z, x, y, color, style):
    path = os.path.join(_CACHE_DIR, f"{style}_{color}", str(z), str(x), f"{y}.png")
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        pass
    png = render_tile(z, x, y, _GPKG_FILES, labels=False, color=color, style=style)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, path)
    except OSError:
        pass
    return png


# Ingen förrendering vid start — den ströp CPU:n och gjorde kartan seg.
# Tiles renderas vid första visningen (~50 ms) och ligger sedan i diskcachen.

# Vektor-endpoint: skickar gränslinjerna som GeoJSON för det synliga området.
# Konverterade koordinater cachas i RAM per feature (fid) så att panorering
# i samma område bara kostar en rtree-slagning + serialisering.
_VEC_CACHE = {}          # fid -> (objekttyp, [[[lon,lat], ...], ...])
_VEC_LIMIT = 30000       # skydd mot för stora bbox-frågor

@app.route("/granser/vector")
def granser_vector():
    try:
        minlat = float(request.args["minlat"])
        minlon = float(request.args["minlon"])
        maxlat = float(request.args["maxlat"])
        maxlon = float(request.args["maxlon"])
    except (KeyError, ValueError):
        return jsonify({"error": "missing bbox"}), 400, _CORS
    if (maxlat - minlat) * (maxlon - minlon) > 0.5:
        return jsonify({"error": "bbox too large"}), 400, _CORS

    e_w, n_s = _wgs84_to_sweref(minlon, minlat)
    e_e, n_n = _wgs84_to_sweref(maxlon, maxlat)

    feats = []
    for gpkg_path in _GPKG_FILES:
        try:
            conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT g.fid, g.objekttyp, g.geom FROM registerenhetsomradesgrans g "
                "JOIN rtree_registerenhetsomradesgrans_geom r ON g.fid = r.id "
                "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ? "
                "LIMIT ?",
                (e_e, e_w, n_n, n_s, _VEC_LIMIT)
            )
            for fid, objtyp, blob in cur.fetchall():
                cached = _VEC_CACHE.get(fid)
                if cached is None:
                    lines = []
                    for coords in _parse_gpkg_geom(blob):
                        line = []
                        for e, n in coords:
                            lat, lon = _sweref_to_wgs84(e, n)
                            line.append([round(lon, 5), round(lat, 5)])
                        if len(line) >= 2:
                            lines.append(line)
                    cached = (objtyp or "", lines)
                    _VEC_CACHE[fid] = cached
                objtyp, lines = cached
                if lines:
                    feats.append({
                        "type": "Feature",
                        "properties": {"t": objtyp},
                        "geometry": {"type": "MultiLineString", "coordinates": lines},
                    })
            conn.close()
        except Exception:
            pass

    body = json.dumps({"type": "FeatureCollection", "features": feats},
                      separators=(",", ":")).encode()
    hdrs = dict(_CORS)
    hdrs["Cache-Control"] = "public, max-age=3600"
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        body = gzip.compress(body, 6)
        hdrs["Content-Encoding"] = "gzip"
    return Response(body, content_type="application/json", headers=hdrs)


_GRANSER_HEADERS = dict(_CORS)
_GRANSER_HEADERS["Cache-Control"] = "public, max-age=604800"  # 7 dagar i webbläsaren

@app.route("/granser/<int:z>/<int:x>/<int:y>.png")
def granser(z, x, y):
    """Bara gränslinjer + skraffering, inga beteckningar."""
    color = (request.args.get("color") or "ffdc00").lower()
    style = request.args.get("style", "dashed")
    if style not in _VALID_STYLES or not all(c in "0123456789abcdef" for c in color) or len(color) != 6:
        return Response("bad params", status=400, headers=_CORS)
    png = _granser_png(z, x, y, color, style)
    return Response(png, content_type="image/png", headers=_GRANSER_HEADERS)


@app.route("/etiketter/<int:z>/<int:x>/<int:y>.png")
def etiketter(z, x, y):
    """Bara fastighetsbeteckningar, inga linjer."""
    png = render_tile(z, x, y, _GPKG_FILES, lines=False, hatch=False)
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


@app.route("/granser/query")
def granser_query():
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "missing lat/lon"}), 400
    result = query_parcel(lat, lon, _GPKG_FILES)
    return jsonify({"beteckning": result}), 200, _CORS


@app.route("/satellite/<int:z>/<int:x>/<int:y>.png")
def satellite(z, x, y):
    url = GOOGLE_HYBRID_URL.format(z=z, x=x, y=y)
    r = requests.get(url, headers=_GOOGLE_HEADERS, timeout=10)
    hdrs = dict(_CORS)
    if r.status_code == 200:
        hdrs["Cache-Control"] = "public, max-age=604800"
    return Response(r.content, content_type="image/png", status=r.status_code, headers=hdrs)


@app.route("/draw")
def draw():
    return send_from_directory(".", "draw.html")


@app.route("/jakt")
def jakt():
    return send_from_directory(".", "jakt.html")


@app.route("/jaktmap")
def jaktmap():
    return send_from_directory(".", "jaktmap.html")


@app.route("/jaktmap/api/geofences")
def jaktmap_geofences():
    r = requests.get(f"{_TRACCAR_URL}/api/geofences", auth=_TRACCAR_AUTH, timeout=5)
    return Response(r.content, content_type="application/json", headers=_CORS)


@app.route("/jaktmap/api/positions")
def jaktmap_positions():
    r = requests.get(f"{_TRACCAR_URL}/api/positions", auth=_TRACCAR_AUTH, timeout=5)
    return Response(r.content, content_type="application/json", headers=_CORS)


@app.route("/jaktmap/api/devices")
def jaktmap_devices():
    r = requests.get(f"{_TRACCAR_URL}/api/devices", auth=_TRACCAR_AUTH, timeout=5)
    return Response(r.content, content_type="application/json", headers=_CORS)


_GUIDE_PASS = os.environ.get("GUIDE_PASS")

# Uppgifterna i appguiden fylls i från miljövariabler så att inga
# adresser eller kontouppgifter ligger i källkoden.
_SETUP_VARS = {
    "{{CLIENT_URL}}":  os.environ.get("SETUP_CLIENT_URL",  ""),
    "{{MANAGER_URL}}": os.environ.get("SETUP_MANAGER_URL", ""),
    "{{JAKT_URL}}":    os.environ.get("SETUP_JAKT_URL",    ""),
    "{{SHARED_USER}}": os.environ.get("SETUP_SHARED_USER", ""),
    "{{SHARED_PASS}}": os.environ.get("SETUP_SHARED_PASS", ""),
}

@app.route("/jaktlag-setup")
def jaktlag_setup():
    if not _GUIDE_PASS:
        return Response("Appguiden är inte konfigurerad (sätt GUIDE_PASS).", status=503)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            _, pwd = decoded.split(":", 1)
            if pwd == _GUIDE_PASS:
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "jaktlag-setup.html")
                with open(path, encoding="utf-8") as fh:
                    html = fh.read()
                for key, val in _SETUP_VARS.items():
                    html = html.replace(key, val)
                return Response(html, content_type="text/html; charset=utf-8")
        except Exception:
            pass
    return Response(
        "Ange lösenord för att visa appguiden.",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Appguide"'},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8085, threaded=True)
