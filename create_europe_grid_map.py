"""
Create a high-resolution map of the European electricity transmission grid.

Data:   OpenStreetMap power lines and cables (ODbL, (c) OpenStreetMap contributors), read from the
        Overture Maps 'base/infrastructure' layer with DuckDB.
        Natural Earth 1:10m countries and lakes (public domain) for land, coasts and borders.
Output: PNG (white and transparent), SVG and PDF in ./output, plus line statistics and a run log.

Usage (from the repository folder):
    python create_europe_grid_map.py              # uses cached data where available
    python create_europe_grid_map.py --variants   # also EEA-highlight and dark versions
    python create_europe_grid_map.py --refresh    # download all source data again
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from shapely import box, get_coordinates  # noqa: E402

# =============================================================================
# CONFIGURATION
# =============================================================================

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
RAW_OVERTURE_DIR = DATA_DIR / "raw" / "overture"
RAW_NE_DIR = DATA_DIR / "raw" / "naturalearth"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = PROJECT_DIR / "output"

# --- Projection and extent --------------------------------------------------
# ETRS89 / LAEA Europe: the EU's standard equal-area projection for pan-European maps.
MAP_CRS = "EPSG:3035"
# Square view window in MAP_CRS metres (xmin, ymin, xmax, ymax):
# Iceland top-left, Portugal bottom-left, Cyprus/Türkiye bottom-right, Moscow near the right edge.
MAP_EXTENT = (2_520_000, 1_330_000, 6_720_000, 5_530_000)

# --- Geographic scope ---------------------------------------------------------
# Lines are kept when they touch one of these countries (Natural Earth codes) or lie entirely at sea,
# so submarine interconnectors stay whole while grids in North Africa / the Middle East are left out.
FOCUS_CONTINENTS = ["Europe"]
FOCUS_EXTRA_ADM0 = ["TUR", "CYP", "CYN"]   # Türkiye, Cyprus, Northern Cyprus (Natural Earth: Asia)
FOCUS_EXCLUDE_ADM0: list[str] = []
DRAW_NON_FOCUS_LAND = False               # False: North Africa etc. is not drawn at all

# --- Colours ------------------------------------------------------------------
# Landsvirkjun brand colours, https://brand.landsvirkjun.is/litir (retrieved 2026-10-22).
# "Vatn": HEX #0091EB, RGB 0/145/235, CMYK 100/37/0/0, Pantone 285.
VATN_COLOR = "#0091EB"
BACKGROUND_COLOR = "#FFFFFF"          # Hvítur
LAND_COLOR = "#F1F4F6"                # very light neutral (between Grágrýti L1 and L2)
COAST_BORDER_COLOR = "#D7E1E6"        # Grágrýti L1
COAST_BORDER_WIDTH = 0.3              # points
DRAW_LAKES = True

# --- Electricity-line selection ----------------------------------------------
MIN_VOLTAGE_KV = 110.0                # lowest voltage plotted (inclusive); OSM voltage is in volts
MAX_VOLTAGE_KV = None                 # optional upper limit, e.g. 400.0
INCLUDE_CABLES = True                 # power=cable (underground / submarine, incl. HVDC links)
# power=line ways without a voltage tag are kept in the processed data as "unknown" but not plotted
# by default, because without a voltage they may be distribution lines.
INCLUDE_UNKNOWN_VOLTAGE_LINES = False
# 16.7 Hz railway traction-current lines (DE/AT/CH/SE/NO) are a separate grid; excluded by default.
INCLUDE_RAILWAY_TRACTION = False
# OSM ways whose lifecycle tag (construction=yes etc.) is known to be outdated, with the reason.
# The geometry still comes from OSM unchanged. Checked against the OSM snapshot of 2026-10-04.
LIFECYCLE_OVERRIDES = {
    "w370147304": "NordLink HVDC cable Tonstad (NO) - Wilster (DE), 525 kV, in commercial operation since "
                  "2021 (Statnett/TenneT); OSM still tags it construction=yes",
}
# OSM ways deliberately left out of the map, with the reason.
EXCLUDE_OSM_IDS = {
    "w641637114": "Valhall power-from-shore cable, Lista (NO) - Valhall platform, 150 kV DC; ends at an "
                  "oil platform mid-sea and reads as a broken NO-UK interconnector",
}

# --- Styling ------------------------------------------------------------------
# Modest width steps per voltage class (points). All classes use VATN_COLOR.
VOLTAGE_CLASSES = [  # (label, lower kV inclusive, upper kV exclusive, line width pt)
    ("110-219 kV", 0.0, 220.0, 0.22),
    ("220-329 kV", 220.0, 330.0, 0.38),
    ("330+ kV", 330.0, math.inf, 0.55),
]
UNKNOWN_VOLTAGE_WIDTH = 0.22
LINE_ALPHA = 0.9
USE_VOLTAGE_WIDTHS = True             # False -> every line uses UNIFORM_LINE_WIDTH
UNIFORM_LINE_WIDTH = 0.35

# --- Output -------------------------------------------------------------------
FIG_WIDTH_IN = 12.0                   # height follows MAP_EXTENT aspect ratio
PNG_DPI = 500                         # 12 in x 500 dpi = 6000 px wide
# Vector outputs are simplified with this tolerance (metres). 1 px at 500 dpi is ~700 m on the map,
# so 150 m is invisible but keeps SVG/PDF sizes manageable.
VECTOR_SIMPLIFY_M = 150.0
OUTPUT_BASENAME = "europe_electricity_grid"

# --- Data sources ---------------------------------------------------------------
# Overture Maps 'base/infrastructure' is a monthly copy of OSM power features (original tags kept
# in source_tags), hosted as GeoParquet on AWS S3. Pin the release for reproducibility; newer
# releases are listed at https://docs.overturemaps.org/release/
OVERTURE_RELEASE = "2026-10-21.0"
OVERTURE_URL = "s3://overturemaps-us-west-2/release/{release}/theme=base/type=infrastructure/*"
NE_COUNTRIES_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
NE_LAKES_URL = "https://naciscdn.org/naturalearth/10m/physical/ne_10m_lakes.zip"
LAKE_MIN_SCALERANK = 3                # only larger lakes (Natural Earth scalerank <= this)
HTTP_USER_AGENT = "europe-grid-map/1.0"

# EEA membership for the optional highlight variant (Natural Earth ADM0_A3 codes).
EU_ADM0 = ["AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC",
           "HUN", "IRL", "ITA", "LVA", "LTU", "LUX", "MLT", "NLD", "POL", "PRT", "ROU", "SVK",
           "SVN", "ESP", "SWE"]
EEA_EFTA_ADM0 = ["ISL", "NOR", "LIE"]
ISOLATED_ADM0 = ["ISL", "CYP", "CYN"]  # not synchronously or HVDC-connected to the continental grid

KEEP_TAGS = ["power", "voltage", "frequency", "line", "location", "cables", "circuits",
             "operator", "name", "ref"]
LIFECYCLE_TAGS = ["construction", "proposed", "planned", "disused", "abandoned", "removed",
                  "razed", "demolished"]

log = logging.getLogger("gridmap")


# =============================================================================
# UTILITIES
# =============================================================================

def setup_logging() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(OUTPUT_DIR / "run_log.txt", mode="w", encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)


def short_hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:10]


def download_file(url: str, dest: Path, refresh: bool = False) -> Path:
    """Download url to dest unless it already exists."""
    if dest.exists() and not refresh:
        log.info("Using cached %s", dest.relative_to(PROJECT_DIR))
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s", url)
    r = requests.get(url, headers={"User-Agent": HTTP_USER_AGENT}, timeout=300)
    r.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(r.content)
    tmp.replace(dest)
    return dest


def view_box():
    return box(*MAP_EXTENT)


def view_lonlat_bounds() -> tuple[int, int, int, int]:
    """Whole-degree lon/lat envelope of the projected view window."""
    # Densify first: the straight top edge in LAEA bulges to ~72 N in lon/lat, beyond its corners.
    view_ll = gpd.GeoSeries([view_box().segmentize(10_000)], crs=MAP_CRS).to_crs(4326)
    w, s, e, n = view_ll.total_bounds
    return math.floor(w), math.floor(s), math.ceil(e), math.ceil(n)


# =============================================================================
# BASE GEOGRAPHY (Natural Earth)
# =============================================================================

def load_countries(refresh: bool = False) -> gpd.GeoDataFrame:
    path = download_file(NE_COUNTRIES_URL, RAW_NE_DIR / "ne_10m_admin_0_countries.zip", refresh)
    gdf = gpd.read_file(f"zip://{path}")[["ADM0_A3", "ADMIN", "CONTINENT", "geometry"]]
    focus = (gdf["CONTINENT"].isin(FOCUS_CONTINENTS) | gdf["ADM0_A3"].isin(FOCUS_EXTRA_ADM0)) \
        & ~gdf["ADM0_A3"].isin(FOCUS_EXCLUDE_ADM0)
    gdf["is_focus"] = focus
    log.info("Natural Earth countries: %d total, %d in focus area", len(gdf), int(focus.sum()))
    # Clip to the map area and project once: the full world polygons (all of Russia to the Pacific)
    # make every later buffer/join/plot step slow.
    w, s, e, n = view_lonlat_bounds()
    gdf = gdf.clip(box(w - 5, max(s - 5, -89), e + 5, min(n + 5, 89))).to_crs(MAP_CRS)
    return gdf[~gdf.geometry.is_empty].reset_index(drop=True)


def load_lakes(refresh: bool = False) -> gpd.GeoDataFrame | None:
    if not DRAW_LAKES:
        return None
    try:
        path = download_file(NE_LAKES_URL, RAW_NE_DIR / "ne_10m_lakes.zip", refresh)
    except requests.RequestException as exc:
        log.warning("Lakes unavailable (%s); continuing without them", exc)
        return None
    lakes = gpd.read_file(f"zip://{path}")
    return lakes[lakes["scalerank"] <= LAKE_MIN_SCALERANK][["geometry"]]


# =============================================================================
# DATA ACQUISITION (OpenStreetMap via Overture Maps)
# =============================================================================

def fetch_voltage_regex() -> str:
    """Download pre-filter: any number with at least as many digits as MIN_VOLTAGE_KV in volts.

    e.g. 110 kV -> 110000 V (6 digits) -> '[1-9][0-9]{5}' matches every value >= 100 kV. The exact
    threshold is applied locally, so the download only needs to be a superset.
    """
    digits = len(str(int(MIN_VOLTAGE_KV * 1000)))
    return f"[1-9][0-9]{{{digits - 1}}}"


def acquire_overture(refresh: bool = False) -> Path:
    """Query OSM power lines/cables from the Overture infrastructure layer and cache them locally."""
    import duckdb  # imported here so the rest of the script works without it once data is cached

    w, s, e, n = view_lonlat_bounds()
    rx = fetch_voltage_regex()
    cache_dir = RAW_OVERTURE_DIR / OVERTURE_RELEASE
    out = cache_dir / f"power_lines_{short_hash([w, s, e, n, rx])}.parquet"
    if out.exists() and not refresh:
        log.info("Source: OpenStreetMap via Overture Maps %s (cached %s)", OVERTURE_RELEASE,
                 out.relative_to(PROJECT_DIR))
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    tag_cols = ",\n  ".join(f"source_tags['{t}'] AS \"{t}\"" for t in KEEP_TAGS + LIFECYCLE_TAGS)
    url = OVERTURE_URL.format(release=OVERTURE_RELEASE)
    # High-voltage lines and cables, plus lines without a voltage tag (kept as "unknown").
    # The bbox struct lets DuckDB skip most remote row groups.
    sql = f"""
SELECT id AS overture_id,
  sources[1].record_id AS osm_record,
  sources[1].version AS osm_snapshot,
  class AS overture_class,
  {tag_cols},
  ST_AsWKB(geometry) AS wkb
FROM read_parquet('{url}', hive_partitioning = 1)
WHERE subtype = 'power' AND class IN ('power_line', 'cable')
  AND bbox.xmax >= {w} AND bbox.xmin <= {e} AND bbox.ymax >= {s} AND bbox.ymin <= {n}
  AND ((source_tags['voltage'] IS NOT NULL AND regexp_matches(source_tags['voltage'], '{rx}'))
       OR (class = 'power_line' AND source_tags['voltage'] IS NULL))"""
    log.info("Source: OpenStreetMap via Overture Maps release %s, bbox lon %d..%d lat %d..%d",
             OVERTURE_RELEASE, w, e, s, n)
    log.info("  querying %s (takes several minutes)", url)
    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
    tmp = out.with_suffix(".part")
    con.execute(f"COPY ({sql}) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
    tmp.replace(out)
    n_rows = con.execute(f"SELECT count(*) FROM '{out.as_posix()}'").fetchone()[0]
    meta = {"release": OVERTURE_RELEASE, "url": url, "query": sql, "rows": n_rows,
            "retrieved_utc": datetime.now(timezone.utc).isoformat()}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("  %d features in %.0f s -> %s", n_rows, time.time() - t0, out.relative_to(PROJECT_DIR))
    return out


# =============================================================================
# CLEANING
# =============================================================================

_num_re = re.compile(r"\d+(?:\.\d+)?")


def parse_voltage_kv(raw) -> float:
    """Highest voltage in kV from an OSM voltage tag ('400000;220000' -> 400). NaN if absent/unparseable.

    OSM voltage is in volts; values written with a kV unit are converted. No values are invented.
    """
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return math.nan
    best = math.nan
    for part in re.split(r"[;,/]", str(raw)):
        m = _num_re.search(part)
        if not m:
            continue
        v = float(m.group())
        v = v if "kv" in part.lower() else v / 1000.0
        best = v if math.isnan(best) else max(best, v)
    return best


def lifecycle_active(value) -> bool:
    """True if a lifecycle tag applies to the whole way.

    Per-circuit values such as disused='no;yes' mean at least one circuit is still in use, so they do
    not remove the line.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return "no" not in [p.strip() for p in str(value).split(";")]


def read_overture(path: Path) -> tuple[gpd.GeoDataFrame, dict]:
    df = pd.read_parquet(path)
    geom = gpd.GeoSeries.from_wkb(df.pop("wkb"), crs=4326)
    # record_id looks like 'w874009361@2' (OSM way id @ version)
    df["osm_id"] = df["osm_record"].str.split("@").str[0]
    active = pd.DataFrame({t: df[t].map(lifecycle_active) for t in LIFECYCLE_TAGS})
    df["lifecycle"] = active.apply(lambda r: ";".join(r.index[r]), axis=1)
    gdf = gpd.GeoDataFrame(df[["osm_id"] + KEEP_TAGS + ["lifecycle"]], geometry=geom)
    meta = {"source": f"Overture Maps {OVERTURE_RELEASE} (OpenStreetMap)",
            "osm_snapshots": sorted(df["osm_snapshot"].dropna().unique().tolist())}
    return gdf, meta


def clean_lines(raw: gpd.GeoDataFrame, countries: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, list]:
    steps = []

    def step(name, gdf):
        steps.append((name, len(gdf)))
        log.info("  %-58s %8d", name, len(gdf))
        return gdf

    log.info("Cleaning (feature counts):")
    gdf = step("raw ways from source", raw)
    gdf = step("unique OSM way ids", gdf.drop_duplicates("osm_id"))
    gdf = step("valid, non-empty geometry (>= 2 vertices)",
               gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty & gdf.geometry.is_valid])
    gdf = step("not busbar/bay (substation internals)", gdf[~gdf["line"].isin(["busbar", "bay"])])
    for oid in gdf.loc[gdf["osm_id"].isin(list(EXCLUDE_OSM_IDS)), "osm_id"]:
        log.info("  excluded by configuration %s: %s", oid, EXCLUDE_OSM_IDS[oid])
    gdf = step("not in EXCLUDE_OSM_IDS", gdf[~gdf["osm_id"].isin(list(EXCLUDE_OSM_IDS))])
    override = gdf["osm_id"].isin(list(LIFECYCLE_OVERRIDES)) & (gdf["lifecycle"] != "")
    for oid in gdf.loc[override, "osm_id"]:
        log.warning("  lifecycle override for %s: %s", oid, LIFECYCLE_OVERRIDES[oid])
    gdf = step("no active lifecycle tag (construction/disused/...)",
               gdf[(gdf["lifecycle"] == "") | override])

    traction = gdf["frequency"].fillna("").str.contains(r"^16\.7|;16\.7|^16,7", regex=True)
    if not INCLUDE_RAILWAY_TRACTION:
        gdf = step("exclude 16.7 Hz railway traction lines", gdf[~traction])

    gdf = gdf.copy()
    gdf["voltage_kv"] = gdf["voltage"].map(parse_voltage_kv)
    gdf["voltage_known"] = gdf["voltage_kv"].notna()
    unparsed = gdf["voltage"].notna() & ~gdf["voltage_known"]
    if unparsed.any():
        log.warning("  %d ways have an unparseable voltage tag; treated as unknown (e.g. %s)",
                    int(unparsed.sum()), gdf.loc[unparsed, "voltage"].head(3).tolist())

    in_range = gdf["voltage_kv"] >= MIN_VOLTAGE_KV
    if MAX_VOLTAGE_KV is not None:
        in_range &= gdf["voltage_kv"] <= MAX_VOLTAGE_KV
    is_line = gdf["power"] == "line"
    is_cable = gdf["power"] == "cable"
    keep = (is_line & in_range) | (is_cable & in_range & INCLUDE_CABLES)
    gdf["selected"] = keep
    gdf["unknown_line"] = is_line & ~gdf["voltage_known"]
    log.info("  power=line, unknown voltage (kept separately, plotted=%s): %d",
             INCLUDE_UNKNOWN_VOLTAGE_LINES, int(gdf["unknown_line"].sum()))
    gdf = step(f"voltage >= {MIN_VOLTAGE_KV:.0f} kV (lines{' + cables' if INCLUDE_CABLES else ''})"
               " or unknown-voltage line", gdf[keep | gdf["unknown_line"]])

    # Spatial scope: keep features touching a focus country (2 km tolerance for coastal landings) and
    # features lying entirely at sea, then cut to the view window. Selecting by intersection, not
    # clipping to land, keeps submarine cables complete; sea-only ways matter because long
    # interconnectors are often split into several OSM ways whose middle part touches no land
    # (e.g. North Sea Link). Only grids on non-focus land (North Africa, Middle East) are dropped.
    gdf = gdf.to_crs(MAP_CRS)
    land = countries[["is_focus", "geometry"]].copy()
    # Simplify before buffering: 500 m is far below the 2 km tolerance and makes the buffer much faster.
    land["geometry"] = land.simplify(500).buffer(2000)
    j = gpd.sjoin(gdf[["geometry"]], land, predicate="intersects", how="left")
    j["is_focus"] = j["is_focus"].fillna(False).astype(bool)
    touches_focus = j.groupby(level=0)["is_focus"].any()
    at_sea = j.groupby(level=0)["index_right"].count() == 0
    log.info("  of which entirely at sea: %d", int((at_sea & ~touches_focus).sum()))
    gdf = step("touches a focus country, or lies entirely at sea",
               gdf.loc[touches_focus.index[touches_focus | at_sea]])
    gdf = gdf[gdf.intersects(view_box())].copy()
    gdf["geometry"] = gdf.geometry.intersection(view_box())
    gdf = gdf[~gdf.geometry.is_empty]
    # Clipping can turn a way into a MultiLineString (it leaves and re-enters the window).
    gdf = gdf.explode(index_parts=False)
    gdf = gdf[gdf.geom_type == "LineString"]
    gdf = step("clipped to view window (exploded to LineStrings)", gdf)
    gdf = step("drop exact duplicate geometries",
               gdf[~gdf.geometry.normalize().to_wkb().duplicated()])

    # Country attribution (line midpoint), for coverage statistics only.
    mid = gpd.GeoDataFrame(geometry=gdf.geometry.interpolate(0.5, normalized=True), crs=MAP_CRS)
    j = gpd.sjoin(mid, countries[["ADM0_A3", "ADMIN", "geometry"]], predicate="within", how="left")
    j = j[~j.index.duplicated()]
    gdf["country"] = j["ADMIN"].fillna("(sea / outside land)")
    gdf["adm0"] = j["ADM0_A3"].fillna("")
    gdf["length_km"] = gdf.length / 1000.0
    return gdf.reset_index(drop=True), steps


def voltage_class(kv: float) -> str:
    if math.isnan(kv):
        return "unknown"
    for label, lo, hi, _ in VOLTAGE_CLASSES:
        if lo <= kv < hi:
            return label
    return VOLTAGE_CLASSES[-1][0]


def processing_key(raw_path: Path) -> str:
    """Hash of everything that affects the processed lines, so they are rebuilt only when needed."""
    cfg = {k: globals()[k] for k in ["MIN_VOLTAGE_KV", "MAX_VOLTAGE_KV", "INCLUDE_CABLES",
                                     "INCLUDE_RAILWAY_TRACTION", "MAP_CRS", "MAP_EXTENT",
                                     "FOCUS_CONTINENTS", "FOCUS_EXTRA_ADM0", "FOCUS_EXCLUDE_ADM0",
                                     "LIFECYCLE_OVERRIDES", "EXCLUDE_OSM_IDS"]}
    cfg["raw"] = (raw_path.name, raw_path.stat().st_size)
    cfg["v"] = 1
    return short_hash(cfg)


def get_processed_lines(raw_path: Path, countries, reprocess=False) -> gpd.GeoDataFrame:
    out = PROCESSED_DIR / f"europe_power_lines_{processing_key(raw_path)}.gpkg"
    if out.exists() and not reprocess:
        log.info("Using processed lines %s", out.relative_to(PROJECT_DIR))
        return gpd.read_file(out)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    raw, meta = read_overture(raw_path)
    log.info("OSM snapshot(s): %s", ", ".join(meta["osm_snapshots"]))
    lines, steps = clean_lines(raw, countries)
    lines["voltage_class"] = lines["voltage_kv"].map(voltage_class)
    lines.to_file(out, driver="GPKG", layer="power_lines")
    pd.DataFrame(steps, columns=["step", "features"]).to_csv(OUTPUT_DIR / "filter_steps.csv", index=False)
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))
    log.info("Saved processed lines -> %s", out.relative_to(PROJECT_DIR))
    return lines


def plotted_subset(lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    m = lines["selected"].astype(bool)
    if INCLUDE_UNKNOWN_VOLTAGE_LINES:
        m |= lines["unknown_line"].astype(bool)
    return lines[m]


def write_statistics(lines: gpd.GeoDataFrame) -> None:
    sel = plotted_subset(lines)
    by_cls = sel.groupby("voltage_class").agg(features=("osm_id", "size"), km=("length_km", "sum"))
    log.info("Plotted features: %d, %.0f km", len(sel), sel["length_km"].sum())
    for cls, r in by_cls.iterrows():
        log.info("  %-12s %7d features %9.0f km", cls, r["features"], r["km"])
    log.info("  of which power=cable: %d", int((sel["power"] == "cable").sum()))

    def km(df):
        return df.groupby("country")["length_km"].sum().round(0)

    # Per-country coverage, including how many line-km lack a voltage tag (not plotted by default).
    stats = pd.DataFrame({
        "plotted_features": sel.groupby("country").size(),
        "plotted_km": km(sel),
        "km_110_219": km(sel[sel["voltage_class"] == "110-219 kV"]),
        "km_220_329": km(sel[sel["voltage_class"] == "220-329 kV"]),
        "km_330_plus": km(sel[sel["voltage_class"] == "330+ kV"]),
        "unknown_voltage_line_km": km(lines[lines["unknown_line"].astype(bool)]),
        "all_km": km(lines),
    }).fillna(0).sort_values("plotted_km", ascending=False)
    stats["unknown_share_of_km"] = (stats["unknown_voltage_line_km"] / stats["all_km"]).round(3)
    path = OUTPUT_DIR / "line_statistics_by_country.csv"
    stats.to_csv(path)
    log.info("Per-country statistics -> %s", path.relative_to(PROJECT_DIR))


# =============================================================================
# PLOTTING
# =============================================================================

def _segments(geoms, simplify_m: float = 0.0) -> list[np.ndarray]:
    if simplify_m > 0:
        geoms = geoms.simplify(simplify_m, preserve_topology=False)
    segs = []
    for g in geoms:
        if g is None or g.is_empty:
            continue
        parts = g.geoms if g.geom_type.startswith("Multi") else [g]
        segs += [get_coordinates(p) for p in parts if len(p.coords) >= 2]
    return segs


def make_map(lines, countries, lakes, out_paths, *, background=BACKGROUND_COLOR, land=LAND_COLOR,
             edge=COAST_BORDER_COLOR, line_color=VATN_COLOR, alpha=LINE_ALPHA, transparent=False,
             simplify_m=0.0, glow=False, country_fills: dict | None = None):
    xmin, ymin, xmax, ymax = MAP_EXTENT
    fig_h = FIG_WIDTH_IN * (ymax - ymin) / (xmax - xmin)
    fig = plt.figure(figsize=(FIG_WIDTH_IN, fig_h), facecolor=background)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(background)

    vb = view_box().buffer(50_000)
    ctry = countries if DRAW_NON_FOCUS_LAND else countries[countries["is_focus"]]
    ctry = ctry.clip(vb)
    if simplify_m:
        ctry["geometry"] = ctry.simplify(simplify_m)
    colors = [country_fills.get(a, land) if country_fills else land for a in ctry["ADM0_A3"]]
    ctry.plot(ax=ax, color=colors, edgecolor=edge, linewidth=COAST_BORDER_WIDTH, zorder=1)
    if lakes is not None:
        lk = lakes.to_crs(MAP_CRS).clip(vb)
        lk.plot(ax=ax, color=background, edgecolor=edge, linewidth=COAST_BORDER_WIDTH * 0.7, zorder=2)

    sel = plotted_subset(lines)
    classes = [("unknown", UNKNOWN_VOLTAGE_WIDTH)] + [(c[0], c[3]) for c in VOLTAGE_CLASSES]
    for z, (cls, lw) in enumerate(classes):  # low voltage first, so major lines draw on top
        sub = sel[sel["voltage_class"] == cls]
        if sub.empty:
            continue
        lw = lw if USE_VOLTAGE_WIDTHS else UNIFORM_LINE_WIDTH
        segs = _segments(sub.geometry, simplify_m)
        if glow:
            ax.add_collection(LineCollection(segs, colors=line_color, linewidths=lw * 6, alpha=0.06,
                                             capstyle="round", zorder=3 + z))
            ax.add_collection(LineCollection(segs, colors=line_color, linewidths=lw * 2.5, alpha=0.15,
                                             capstyle="round", zorder=3 + z))
        ax.add_collection(LineCollection(segs, colors=line_color, linewidths=lw, alpha=alpha,
                                         capstyle="round", joinstyle="round", zorder=10 + z))

    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal")
    ax.set_axis_off()
    if transparent:
        fig.patch.set_alpha(0)
        ax.patch.set_alpha(0)
    for p in out_paths:
        kw = {"dpi": PNG_DPI} if p.suffix == ".png" else {}
        fig.savefig(p, facecolor="none" if transparent else background, transparent=transparent, **kw)
        log.info("Wrote %s (%.1f MB)", p.relative_to(PROJECT_DIR), p.stat().st_size / 1e6)
    plt.close(fig)


def create_outputs(lines, countries, lakes, variants: bool):
    base = OUTPUT_DIR / OUTPUT_BASENAME
    make_map(lines, countries, lakes, [Path(f"{base}_white.png")])
    make_map(lines, countries, lakes, [Path(f"{base}_transparent.png")], transparent=True)
    make_map(lines, countries, lakes, [Path(f"{base}_white.svg"), Path(f"{base}_white.pdf")],
             simplify_m=VECTOR_SIMPLIFY_M)
    if not variants:
        return
    # EEA highlight: EU + Iceland/Norway/Liechtenstein tinted, isolated systems (Iceland, Cyprus) in
    # light orange. Same Vatn lines; only the country fills differ.
    fills = {a: "#E3EDF2" for a in EU_ADM0 + EEA_EFTA_ADM0}
    fills.update({a: "#FFE6B9" for a in ISOLATED_ADM0})                # Appelsínugulur L2
    make_map(lines, countries, lakes, [Path(f"{base}_eea_isolation.png")], country_fills=fills)
    # Dark version on the Nótt background.
    make_map(lines, countries, lakes, [Path(f"{base}_dark.png")], background="#0A0A46",
             land="#0F0F55", edge="#1E2470", line_color="#3CB4FF", alpha=0.95, glow=True)


# =============================================================================
# MAIN
# =============================================================================

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="download all source data again")
    ap.add_argument("--reprocess", action="store_true", help="rebuild processed lines from cached data")
    ap.add_argument("--variants", action="store_true", help="also create EEA-highlight and dark versions")
    args = ap.parse_args(argv)

    setup_logging()
    log.info("Europe electricity grid map | CRS %s | extent %s | min voltage %.0f kV | colour %s",
             MAP_CRS, MAP_EXTENT, MIN_VOLTAGE_KV, VATN_COLOR)
    countries = load_countries(args.refresh)
    lakes = load_lakes(args.refresh)
    raw_path = acquire_overture(args.refresh)
    lines = get_processed_lines(raw_path, countries, args.reprocess or args.refresh)
    if lines.empty:
        log.error("No lines left after filtering; check configuration")
        sys.exit(1)
    for code, name in [("ISL", "Iceland"), ("GBR", "United Kingdom"), ("IRL", "Ireland"),
                       ("NOR", "Norway"), ("CYP", "Cyprus")]:
        n = int((plotted_subset(lines)["adm0"] == code).sum())
        (log.info if n else log.warning)("  coverage check %-15s %6d plotted features", name, n)
    write_statistics(lines)
    create_outputs(lines, countries, lakes, args.variants)
    log.info("Done. Outputs in %s", OUTPUT_DIR.relative_to(PROJECT_DIR))


if __name__ == "__main__":
    main()
