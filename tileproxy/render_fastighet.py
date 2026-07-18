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

from PIL import Image, ImageDraw, ImageChops, ImageFont

logger = logging.getLogger(__name__)

# Load all .gpkg files found next to this file
_GPKG_DIR = Path(__file__).parent / "fastighet"

_LINE_COLOR = (255, 220, 0, 230)   # yellow, mostly opaque
_LINE_WIDTH = 2
_TILE_SIZE = 256

# Avtalsområden: skiften som inte ingår i jaktmarken men där överenskommelse
# finns (eftersök av fällt/påskjutet vilt). Ritas med diagonal skraffering.
_HATCH_FIDS = (257005628, 257009230)   # RISTA 2:28, RISTA 2:29>1
_HATCH_COLOR = (30, 150, 60, 150)      # grön, halvtransparent
_HATCH_BASE_SPACING = 14               # px vid basezoom — avståndet är markfixerat
_HATCH_BASE_Z = 13                     # vid högre zoom växer pixelavståndet (2x per nivå)
_HATCH_LINE_WIDTH = 2
_hatch_rings_cache = None

# Fastighetsbeteckningar (etiketter) ritas från denna zoomnivå
_LABEL_MIN_Z = 14
_LABEL_COLOR = (120, 70, 0, 255)       # mörkt brungult — matchar gränslinjerna
_LABEL_HALO = (255, 255, 255, 220)
_TRAKT_MIN_Z = 13
_TRAKT_COLOR = (90, 90, 90, 255)
_font_cache = {}


def _get_font(size):
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "C:/Windows/Fonts/arial.ttf"):
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def _ring_centroid(ring):
    """Area-weighted polygon centroid (shoelace)."""
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-9:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    return cx / (3 * a), cy / (3 * a)


def _draw_text_halo(draw, pos, text, font, fill):
    x, y = pos
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=_LABEL_HALO, anchor="mm")
    draw.text((x, y), text, font=font, fill=fill, anchor="mm")


def _draw_labels(img, z, to_px, gpkg_paths, q_bbox):
    """Draw parcel etiketter and trakt names anchored at polygon centroids."""
    if z < _TRAKT_MIN_Z:
        return
    q_e_w, q_n_s, q_e_e, q_n_n = q_bbox
    draw = ImageDraw.Draw(img)

    jobs = []  # (table, font, color)
    if z >= _LABEL_MIN_Z:
        jobs.append(("registerenhetsomradesyta", _get_font(11 if z == 14 else 13), _LABEL_COLOR))
    jobs.append(("traktyta", _get_font(12 if z == _TRAKT_MIN_Z else 15), _TRAKT_COLOR))

    for gpkg_path in gpkg_paths:
        try:
            conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
            cur = conn.cursor()
            for table, font, color in jobs:
                cur.execute(
                    f"SELECT t.etikett, t.geom FROM {table} t "
                    f"JOIN rtree_{table}_geom r ON t.fid = r.id "
                    "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ?",
                    (q_e_e, q_e_w, q_n_n, q_n_s)
                )
                for etikett, blob in cur.fetchall():
                    if not etikett:
                        continue
                    rings = _parse_gpkg_polygon(blob)
                    if not rings:
                        continue
                    ce, cn = _ring_centroid(rings[0])
                    px, py = to_px(ce, cn)
                    if -80 <= px <= _TILE_SIZE + 80 and -20 <= py <= _TILE_SIZE + 20:
                        _draw_text_halo(draw, (px, py), etikett, font, color)
            conn.close()
        except Exception as exc:
            logger.warning("Label error %s: %s", gpkg_path, exc)


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


def _sweref_to_wgs84(easting, northing):
    """Inverse transverse Mercator: SWEREF 99 TM → WGS84 lat/lon (iterative)."""
    a = 6378137.0
    f = 1 / 298.257222101
    lon0 = 15.0
    k0 = 0.9996
    fe = 500000.0

    b = a * (1 - f)
    e2 = 1 - (b / a) ** 2

    A0 = 1 - e2/4 - 3*e2**2/64 - 5*e2**3/256
    A2 = 3/8 * (e2 + e2**2/4 + 15*e2**3/128)
    A4 = 15/256 * (e2**2 + 3*e2**3/4)
    A6 = 35*e2**3/3072

    def meridian_arc(lat_r):
        return a * (A0*lat_r - A2*math.sin(2*lat_r) + A4*math.sin(4*lat_r) - A6*math.sin(6*lat_r))

    # Footpoint latitude by Newton iteration
    M = northing / k0
    lat_r = M / (a * A0)
    for _ in range(6):
        lat_r += (M - meridian_arc(lat_r)) / (a * A0)

    de = easting - fe
    N = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    t = math.tan(lat_r)
    eta2 = (e2 / (1 - e2)) * math.cos(lat_r) ** 2
    d = de / (k0 * N)

    lat = lat_r - (t * (1 + eta2) / 2) * d**2 \
        + (t * (5 + 3*t**2 + 6*eta2 - 6*t**2*eta2) / 24) * d**4 \
        - (t * (61 + 90*t**2 + 45*t**4) / 720) * d**6
    lon = math.radians(lon0) + (d
        - ((1 + 2*t**2 + eta2) / 6) * d**3
        + ((5 + 28*t**2 + 24*t**4) / 120) * d**5) / math.cos(lat_r)

    return math.degrees(lat), math.degrees(lon)


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


def _load_hatch_rings(gpkg_paths):
    """Load and cache SWEREF rings for the agreement parcels."""
    global _hatch_rings_cache
    if _hatch_rings_cache is not None:
        return _hatch_rings_cache
    result = []
    for gpkg_path in gpkg_paths:
        try:
            conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
            cur = conn.cursor()
            for fid in _HATCH_FIDS:
                cur.execute("SELECT geom FROM registerenhetsomradesyta WHERE fid = ?", (fid,))
                row = cur.fetchone()
                if row:
                    rings = _parse_gpkg_polygon(row[0])
                    if rings:
                        result.append(rings)
            conn.close()
        except Exception as exc:
            logger.warning("Hatch load error %s: %s", gpkg_path, exc)
    _hatch_rings_cache = result
    return result


def _parse_wkb_polygon_at(wkb, offset):
    """Parse one WKB Polygon at offset. Returns (rings, new_offset)."""
    endian = '<' if wkb[offset] == 1 else '>'
    offset += 5  # byte order + geom type
    n_rings = struct.unpack_from(endian + 'I', wkb, offset)[0]
    offset += 4
    rings = []
    for _ in range(n_rings):
        n_pts = struct.unpack_from(endian + 'I', wkb, offset)[0]
        offset += 4
        coords = struct.unpack_from(endian + f'{n_pts*2}d', wkb, offset)
        offset += n_pts * 16
        rings.append(list(zip(coords[::2], coords[1::2])))
    return rings, offset


def _ring_area(ring):
    return abs(sum(ring[i][0] * ring[i+1][1] - ring[i+1][0] * ring[i][1]
                   for i in range(len(ring) - 1))) / 2


def _parse_gpkg_polygon(blob):
    """Parse a GeoPackage Polygon/MultiPolygon blob into a list of rings.
    For MultiPolygon the largest part is returned (used for label anchors)."""
    if not blob or len(blob) < 8 or blob[0:2] != b'GP':
        return []
    flags = blob[3]
    envelope_type = (flags >> 1) & 0x07
    envelope_sizes = [0, 32, 48, 48, 64]
    header_len = 8 + envelope_sizes[envelope_type] if envelope_type < 5 else 8
    wkb = blob[header_len:]
    if len(wkb) < 9:
        return []
    endian = '<' if wkb[0] == 1 else '>'
    geom_type = struct.unpack_from(endian + 'I', wkb, 1)[0]
    if geom_type == 3:  # Polygon
        rings, _ = _parse_wkb_polygon_at(wkb, 0)
        return rings
    if geom_type == 6:  # MultiPolygon — take the largest part
        n_polys = struct.unpack_from(endian + 'I', wkb, 5)[0]
        offset = 9
        best, best_area = [], -1.0
        for _ in range(n_polys):
            rings, offset = _parse_wkb_polygon_at(wkb, offset)
            if rings:
                area = _ring_area(rings[0])
                if area > best_area:
                    best, best_area = rings, area
        return best
    return []


def _draw_hatch(img, z, x, y, to_px, gpkg_paths):
    """Draw seamless diagonal hatching over the agreement parcels."""
    rings_list = _load_hatch_rings(gpkg_paths)
    if not rings_list:
        return
    mask = Image.new("L", (_TILE_SIZE, _TILE_SIZE), 0)
    mdraw = ImageDraw.Draw(mask)
    hit = False
    for rings in rings_list:
        outer = [to_px(e, n) for e, n in rings[0]]
        xs = [p[0] for p in outer]
        ys = [p[1] for p in outer]
        if max(xs) < 0 or min(xs) > _TILE_SIZE or max(ys) < 0 or min(ys) > _TILE_SIZE:
            continue
        mdraw.polygon(outer, fill=255)
        for hole in rings[1:]:
            mdraw.polygon([to_px(e, n) for e, n in hole], fill=0)
        # outline so the parcel edge is visible in the same color
        pixels = outer + [outer[0]]
        ImageDraw.Draw(img).line(pixels, fill=_HATCH_COLOR, width=_HATCH_LINE_WIDTH)
        hit = True
    if not hit:
        return
    hatch = Image.new("RGBA", (_TILE_SIZE, _TILE_SIZE), (0, 0, 0, 0))
    hdraw = ImageDraw.Draw(hatch)
    # Markfixerat avstånd: dubblas för varje zoomnivå över basen så att
    # ränderna glesas ut när man zoomar in i stället för att förtätas
    spacing = max(8, _HATCH_BASE_SPACING * 2 ** (z - _HATCH_BASE_Z))
    # 45°-linjer där (global_x + global_y) % spacing == 0 — sömlöst mellan tiles
    start = (-(x + y) * _TILE_SIZE) % spacing
    for k in range(start, 2 * _TILE_SIZE, spacing):
        hdraw.line([(k, 0), (0, k)], fill=_HATCH_COLOR, width=_HATCH_LINE_WIDTH)
    hatch.putalpha(ImageChops.multiply(hatch.getchannel("A"), mask))
    img.alpha_composite(hatch)


def _hex_to_rgba(hex_color: str, alpha: int = 230) -> tuple:
    """Convert '#rrggbb' to (r, g, b, alpha)."""
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = ''.join(c*2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r, g, b, alpha)


# LM style: line appearance per objekttyp
_LM_STYLES = {
    "traktgräns":        {"width": 3,   "dash": None},
    "kommungräns":       {"width": 3,   "dash": None},
    "riksgräns":         {"width": 3,   "dash": None},
    "fastighetsgräns":   {"width": 1.5, "dash": (6, 5)},
    "kvarterstraktgräns":{"width": 1.5, "dash": (2, 4)},
    "fastighetsstrand":  {"width": 1.5, "dash": (2, 4)},
    "fiskeområdesgräns": {"width": 1,   "dash": (4, 6)},
}


_DASH_MARGIN = 8  # px outside the tile still drawn so dashes meet at tile edges


def _clip_seg_params(x0, y0, dx, dy, seg):
    """Liang-Barsky: param range [t0, t1] of the segment inside the padded tile,
    or None if fully outside."""
    lo, hi = -_DASH_MARGIN, _TILE_SIZE + _DASH_MARGIN
    t0, t1 = 0.0, seg
    for p, q_lo, q_hi in ((dx, lo - x0, hi - x0), (dy, lo - y0, hi - y0)):
        if p == 0:
            if q_lo > 0 or q_hi < 0:
                return None
            continue
        ta, tb = q_lo / p, q_hi / p
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1:
            return None
    return t0, t1


def _draw_dashed_line(draw, pixels, color, width, dash):
    """Draw a dashed/dotted polyline. dash = (on, off) in pixels.

    Dash phase follows cumulative distance along the whole polyline, so the
    pattern is continuous. Segments (and parts of segments) outside the tile
    are skipped in O(1) — only visible length costs iterations.
    """
    if not dash or len(pixels) < 2:
        draw.line(pixels, fill=color, width=int(width))
        return
    on, off = dash
    period = on + off
    w = int(width)
    t = 0.0  # cumulative distance along the polyline
    for i in range(len(pixels) - 1):
        x0, y0 = pixels[i]
        x1, y1 = pixels[i + 1]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg == 0:
            continue
        dx, dy = (x1 - x0) / seg, (y1 - y0) / seg
        clip = _clip_seg_params(x0, y0, dx, dy, seg)
        if clip is None:
            t += seg
            continue
        p0, p1 = clip
        # iterate whole dash periods by integer index — cannot stall on
        # float rounding the way incremental accumulation can
        k0 = int(math.floor((t + p0) / period))
        k1 = int(math.floor((t + p1) / period))
        for k in range(k0, k1 + 1):
            ds = k * period - t          # dash start along this segment
            cs = ds if ds > p0 else p0
            ce = ds + on
            if ce > p1:
                ce = p1
            if cs < ce:
                draw.line([(x0 + dx * cs, y0 + dy * cs),
                           (x0 + dx * ce, y0 + dy * ce)],
                          fill=color, width=w)
        t += seg


def render_tile(z: int, x: int, y: int, gpkg_paths: list,
                lines: bool = True, hatch: bool = True, labels: bool = True,
                color: str = None, style: str = "dashed") -> bytes:
    """Render fastighetsgränser for tile (z, x, y) as transparent PNG bytes.

    Args:
        color:  hex color string e.g. 'ffdc00' (without #). None = use default yellow.
        style:  'dashed' | 'solid' | 'lm'
    """
    line_color = _hex_to_rgba('#' + color) if color else _LINE_COLOR

    e_w, n_s, e_e, n_n, lon_w, lat_s, lon_e, lat_n = _tile_to_bbox_sweref(z, x, y)

    pad = (e_e - e_w) * 0.05
    q_e_w, q_e_e = e_w - pad, e_e + pad
    q_n_s, q_n_n = n_s - pad, n_n + pad

    img = Image.new("RGBA", (_TILE_SIZE, _TILE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    n_tiles = 2 ** z
    world = 20037508.34
    tile_mx_w = (x / n_tiles) * 2 * world - world
    tile_my_n = world - (y / n_tiles) * 2 * world
    merc_per_px = (2 * world / n_tiles) / _TILE_SIZE

    def to_px(easting, northing):
        lat, lon = _sweref_to_wgs84(easting, northing)
        mx = lon * world / 180.0
        my = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137.0
        return (mx - tile_mx_w) / merc_per_px, (tile_my_n - my) / merc_per_px

    drawn = 0
    if lines:
        for gpkg_path in gpkg_paths:
            try:
                conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute(
                    "SELECT g.geom, g.objekttyp FROM registerenhetsomradesgrans g "
                    "JOIN rtree_registerenhetsomradesgrans_geom r ON g.fid = r.id "
                    "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ?",
                    (q_e_e, q_e_w, q_n_n, q_n_s)
                )
                for (blob, objtyp) in cur.fetchall():
                    for coords in _parse_gpkg_geom(blob):
                        if len(coords) < 2:
                            continue
                        pixels = [to_px(e, n) for e, n in coords]
                        if style == "lm":
                            lm = _LM_STYLES.get(objtyp or "", {"width": _LINE_WIDTH, "dash": (5, 5)})
                            c = _hex_to_rgba('#' + color) if color else _LINE_COLOR
                            _draw_dashed_line(draw, pixels, c, lm["width"], lm["dash"])
                        elif style == "solid":
                            draw.line(pixels, fill=line_color, width=_LINE_WIDTH)
                        else:  # dashed (default)
                            _draw_dashed_line(draw, pixels, line_color, _LINE_WIDTH, (5, 5))
                        drawn += 1
                conn.close()
            except Exception as exc:
                logger.warning("GeoPackage error %s: %s", gpkg_path, exc)

    if hatch:
        _draw_hatch(img, z, x, y, to_px, gpkg_paths)

    if labels:
        # Etiketter: bredare sökruta så text vars ankare ligger i granntile också ritas
        lpad = (e_e - e_w) * 0.4
        _draw_labels(img, z, to_px, gpkg_paths,
                     (e_w - lpad, n_s - lpad, e_e + lpad, n_n + lpad))

    logger.debug("Tile %d/%d/%d: drew %d lines", z, x, y, drawn)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def find_gpkg_files():
    return list(_GPKG_DIR.glob("*.gpkg"))


def _point_in_ring(ring, px, py):
    """Ray casting — point (px, py) inside polygon ring."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _lookup_etikett(cur, table, e, n, pad):
    """Return etikett from table for the polygon containing (e, n), or None."""
    cur.execute(
        f"SELECT t.etikett, t.geom FROM {table} t "
        f"JOIN rtree_{table}_geom r ON t.fid = r.id "
        "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ?",
        (e + pad, e - pad, n + pad, n - pad)
    )
    for etikett, blob in cur.fetchall():
        if not etikett:
            continue
        rings = _parse_gpkg_polygon(blob)
        if rings and _point_in_ring(rings[0], e, n):
            return etikett
    return None


def query_parcel(lat, lon, gpkg_paths):
    """Return fastighetsbeteckning for the parcel containing (lat, lon), or None.

    Combines tract name from traktyta ("RISTA") with parcel number from
    registerenhetsomradesyta ("2:5") → "RISTA 2:5".
    """
    e, n = _wgs84_to_sweref(lon, lat)
    pad = 2.0
    for gpkg_path in gpkg_paths:
        try:
            conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
            cur = conn.cursor()
            parcel = _lookup_etikett(cur, "registerenhetsomradesyta", e, n, pad)
            trakt  = _lookup_etikett(cur, "traktyta", e, n, pad)
            conn.close()
            if parcel or trakt:
                parts = [p for p in [trakt, parcel] if p]
                return " ".join(parts)
        except Exception as exc:
            logger.warning("Query error %s: %s", gpkg_path, exc)
    return None
