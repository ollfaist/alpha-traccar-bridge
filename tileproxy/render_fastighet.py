"""
Renders fastighetsindelning boundary lines from a GeoPackage as transparent PNG tiles.
Queries only features within the tile BBOX — no need to pre-render all tiles.
"""

import math
import sqlite3
import struct
import logging
from pathlib import Path
from io import BytesIO

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# Load all .gpkg files found next to this file
_GPKG_DIR = Path(__file__).parent / "fastighet"

_LINE_COLOR = (255, 220, 0, 230)   # yellow, mostly opaque
_LINE_WIDTH = 2
_TILE_SIZE = 256


def _tile_to_bbox_sweref(z, x, y):
    """Convert XYZ tile to bbox in EPSG:3857, then approximate SWEREF 99 TM (EPSG:3006).
    We do a simple WGS84 → SWEREF conversion sufficient for bbox queries.
    """
    n = 2 ** z

    def merc_to_lon(mx): return mx / 20037508.34 * 180.0
    def merc_to_lat(my):
        lat_r = 2 * math.atan(math.exp(my / 6378137.0)) - math.pi / 2
        return math.degrees(lat_r)

    mx_w = (x / n) * 2 * 20037508.34 - 20037508.34
    mx_e = ((x + 1) / n) * 2 * 20037508.34 - 20037508.34
    my_n = 20037508.34 - (y / n) * 2 * 20037508.34
    my_s = 20037508.34 - ((y + 1) / n) * 2 * 20037508.34

    lon_w, lon_e = merc_to_lon(mx_w), merc_to_lon(mx_e)
    lat_s, lat_n = merc_to_lat(my_s), merc_to_lat(my_n)

    # WGS84 → approximate SWEREF 99 TM
    e_w, n_s = _wgs84_to_sweref(lon_w, lat_s)
    e_e, n_n = _wgs84_to_sweref(lon_e, lat_n)
    return e_w, n_s, e_e, n_n, lon_w, lat_s, lon_e, lat_n


def _wgs84_to_sweref(lon, lat):
    """Approximate WGS84 → SWEREF 99 TM (good enough for bbox filtering)."""
    a = 6378137.0
    f = 1 / 298.257222101
    lat0, lon0 = 0.0, 15.0
    k0 = 0.9996
    fe, fn = 500000.0, 0.0

    b = a * (1 - f)
    e2 = 1 - (b / a) ** 2
    e = math.sqrt(e2)
    n_coeff = (a - b) / (a + b)

    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    lon0_r = math.radians(lon0)

    N = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    t = math.tan(lat_r)
    eta2 = (e2 / (1 - e2)) * math.cos(lat_r) ** 2
    dl = lon_r - lon0_r

    A0 = 1 - e2/4 - 3*e2**2/64 - 5*e2**3/256
    A2 = 3/8 * (e2 + e2**2/4 + 15*e2**3/128)
    A4 = 15/256 * (e2**2 + 3*e2**3/4)
    A6 = 35*e2**3/3072
    M = a * (A0*lat_r - A2*math.sin(2*lat_r) + A4*math.sin(4*lat_r) - A6*math.sin(6*lat_r))

    x = k0 * N * (dl*math.cos(lat_r)
        + dl**3/6 * math.cos(lat_r)**3 * (1 - t**2 + eta2)
        + dl**5/120 * math.cos(lat_r)**5 * (5 - 18*t**2 + t**4)) + fe

    y = k0 * (M + N*math.sin(lat_r)*math.cos(lat_r) * (
        dl**2/2
        + dl**4/24 * math.cos(lat_r)**2 * (5 - t**2 + 9*eta2)
        + dl**6/720 * math.cos(lat_r)**4 * (61 - 58*t**2 + t**4))) + fn

    return x, y


def _parse_gpkg_geom(blob):
    """Parse GeoPackage geometry blob and return list of (x, y) coordinate lists."""
    if not blob or len(blob) < 8:
        return []
    # GeoPackage header: 'GP' + version + flags + srs_id + optional envelope
    if blob[0:2] != b'GP':
        return []
    flags = blob[3]
    envelope_type = (flags >> 1) & 0x07
    envelope_sizes = [0, 32, 48, 48, 64]
    header_len = 8 + envelope_sizes[envelope_type] if envelope_type < 5 else 8
    wkb = blob[header_len:]
    return _parse_wkb(wkb)


def _parse_wkb(wkb, offset=0):
    """Parse WKB geometry and return list of coordinate lists (rings/lines)."""
    if len(wkb) < offset + 5:
        return []
    byte_order = wkb[offset]
    endian = '<' if byte_order == 1 else '>'
    geom_type = struct.unpack_from(endian + 'I', wkb, offset + 1)[0]
    offset += 5

    results = []
    if geom_type == 2:  # LineString
        n = struct.unpack_from(endian + 'I', wkb, offset)[0]
        offset += 4
        coords = struct.unpack_from(endian + f'{n*2}d', wkb, offset)
        results.append(list(zip(coords[::2], coords[1::2])))
    elif geom_type == 5:  # MultiLineString
        n = struct.unpack_from(endian + 'I', wkb, offset)[0]
        offset += 4
        for _ in range(n):
            sub = _parse_wkb(wkb, offset)
            results.extend(sub)
            byte_order2 = wkb[offset]
            endian2 = '<' if byte_order2 == 1 else '>'
            n_pts = struct.unpack_from(endian2 + 'I', wkb, offset + 5)[0]
            offset += 5 + 4 + n_pts * 16
    return results


_MARGIN = 8  # extra pixels around tile to avoid seam gaps


def render_tile(z: int, x: int, y: int, gpkg_paths: list) -> bytes:
    """Render fastighetsgränser for tile (z, x, y) as transparent PNG bytes."""
    e_w, n_s, e_e, n_n, lon_w, lat_s, lon_e, lat_n = _tile_to_bbox_sweref(z, x, y)

    # Expand bbox by MARGIN pixels so lines crossing tile edges are drawn fully
    de = (e_e - e_w) / _TILE_SIZE * _MARGIN
    dn = (n_n - n_s) / _TILE_SIZE * _MARGIN
    q_e_w, q_e_e = e_w - de, e_e + de
    q_n_s, q_n_n = n_s - dn, n_n + dn

    canvas = _TILE_SIZE + 2 * _MARGIN
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def to_px(easting, northing):
        px = (easting - q_e_w) / (q_e_e - q_e_w) * canvas
        py = (1 - (northing - q_n_s) / (q_n_n - q_n_s)) * canvas
        return px, py

    drawn = 0
    for gpkg_path in gpkg_paths:
        try:
            conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT g.geom FROM registerenhetsomradesgrans g "
                "JOIN rtree_registerenhetsomradesgrans_geom r ON g.fid = r.id "
                "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ?",
                (q_e_e, q_e_w, q_n_n, q_n_s)
            )
            for (blob,) in cur.fetchall():
                for coords in _parse_gpkg_geom(blob):
                    if len(coords) < 2:
                        continue
                    pixels = [to_px(e, n) for e, n in coords]
                    draw.line(pixels, fill=_LINE_COLOR, width=_LINE_WIDTH)
                    drawn += 1
            conn.close()
        except Exception as exc:
            logger.warning("GeoPackage error %s: %s", gpkg_path, exc)

    # Crop back to exact tile size
    img = img.crop((_MARGIN, _MARGIN, _MARGIN + _TILE_SIZE, _MARGIN + _TILE_SIZE))

    logger.debug("Tile %d/%d/%d: drew %d lines", z, x, y, drawn)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def find_gpkg_files():
    return list(_GPKG_DIR.glob("*.gpkg"))
