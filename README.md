# Europe grid map

A reproducible, high-resolution map of the European electricity transmission grid: OpenStreetMap
power lines and cables of 110 kV and above, drawn in Landsvirkjun **Vatn (#0091EB)** on a white background.

> **Attribution required wherever the map is shown: "© OpenStreetMap contributors"**

## Quick start

Requires Python 3.10+ (tested with 3.12 on Windows).

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

```bash
python create_europe_grid_map.py --variants
```

* **First run:** downloads Natural Earth (about 7 MB), then queries the OSM power lines from Overture Maps on AWS S3 with DuckDB (about 8 minutes, about 300k features), and cleans the data (about 1 minute).
* **Later runs** reuse the cache in `data/` and write all maps in about 1 minute.
* `--refresh` downloads everything again. `--reprocess` rebuilds the cleaned network from the cached download.
* `data/` and `output/` are created by the script and are not part of the repository.

On macOS/Linux, activate the virtual environment with `source .venv/bin/activate`.

## Outputs (`output/`)

| File | Content |
|---|---|
| `europe_electricity_grid_white.png` | Main map, 6000 × 6000 px, white background |
| `europe_electricity_grid_transparent.png` | Same map with a transparent background (land fill kept) |
| `europe_electricity_grid_white.svg` / `.pdf` | Vector versions (lines simplified by 150 m, not visible at this scale) |
| `europe_electricity_grid_eea_isolation.png` | `--variants`: EU + Iceland/Norway/Liechtenstein tinted, isolated systems (Iceland, Cyprus) in light orange |
| `europe_electricity_grid_dark.png` | `--variants`: dark version on the Nótt background |
| `line_statistics_by_country.csv` | Plotted line-km per country and voltage class, plus line-km without a voltage tag |
| `filter_steps.csv` | Feature counts after each cleaning step |
| `run_log.txt` | Log of the last run: source, OSM snapshot date, filters, counts, output files |

## Settings

All settings are at the top of `create_europe_grid_map.py`.

| Setting | Default | Meaning |
|---|---|---|
| `MAP_CRS` | `EPSG:3035` | ETRS89-LAEA Europe (equal-area) |
| `MAP_EXTENT` | `(2520000, 1330000, 6720000, 5530000)` | Square view window in metres: Iceland to Moscow, Nordkapp to Crete and Cyprus |
| `FOCUS_CONTINENTS`, `FOCUS_EXTRA_ADM0` | Europe + TUR, CYP, CYN | Countries whose grids are drawn |
| `VATN_COLOR` | `#0091EB` | Line colour |
| `MIN_VOLTAGE_KV` / `MAX_VOLTAGE_KV` | 110 / None | Voltage filter. Changing the minimum below 100 kV triggers a new, larger download |
| `INCLUDE_CABLES` | True | Underground and submarine cables, incl. HVDC interconnectors |
| `INCLUDE_UNKNOWN_VOLTAGE_LINES` | False | Also draw `power=line` ways without a voltage tag |
| `INCLUDE_RAILWAY_TRACTION` | False | Also draw 16.7 Hz railway traction-current lines |
| `LIFECYCLE_OVERRIDES` | NordLink | OSM ways whose "under construction" or "disused" tag is known to be outdated |
| `EXCLUDE_OSM_IDS` | Valhall cable | OSM ways deliberately left out |
| `VOLTAGE_CLASSES`, `USE_VOLTAGE_WIDTHS` | 0.22 / 0.38 / 0.55 pt | Line width for 110–219, 220–329 and 330+ kV |
| `LINE_ALPHA` | 0.9 | Line opacity |
| `FIG_WIDTH_IN`, `PNG_DPI` | 12 in, 500 dpi | Output size and resolution |
| `OVERTURE_RELEASE` | `2026-10-21.0` | Pinned data release. Use a newer one for newer OSM data |

## How it works

1. **Download.** A DuckDB query on the Overture Maps `base/infrastructure` GeoParquet, filtered by bounding box. It selects `power=line` and `power=cable` with voltage ≥ 100 kV, plus lines without any voltage tag. The result is cached as Parquet.
2. **Clean.** The script then:
   * removes duplicate and invalid geometries;
   * removes substation busbars and bays;
   * removes planned, under-construction and disused features (lifecycle tags), apart from documented overrides. Partly disused multi-circuit lines (`disused=no;yes`) are kept;
   * removes 16.7 Hz railway lines;
   * applies the voltage filter (the highest voltage of multi-voltage ways is used). Missing voltages are kept as "unknown" and never guessed.
3. **Scope.** Features are kept when they touch a focus country, or lie entirely at sea. Long submarine cables are often split into several OSM ways, and their middle pieces touch no land, so this keeps cables such as North Sea Link complete. Features are then clipped to the view window.
4. **Plot.** Matplotlib `LineCollection`s per voltage class, over Natural Earth 1:10m land, coasts, borders and large lakes. No axes, labels or legend.

## Data sources and licences

| Data | Source | Licence / attribution |
|---|---|---|
| Power lines and cables | OpenStreetMap, via [Overture Maps](https://docs.overturemaps.org/) `base/infrastructure` (release 2026-10-21.0, OSM planet snapshot 2026-10-04). Overture keeps the OSM way id (`sources[1].record_id`) and the original tags (`source_tags`) | [ODbL 1.0](https://www.openstreetmap.org/copyright). **"© OpenStreetMap contributors"** is required |
| Land, coasts, borders, lakes | [Natural Earth](https://www.naturalearthdata.com/) 1:10m Admin 0 Countries and Lakes | Public domain |
| Colours | Landsvirkjun brand portal, https://brand.landsvirkjun.is/litir. Vatn #0091EB, Grágrýti L1 #D7E1E6, Appelsínugulur L2 #FFE6B9, Nótt #0A0A46 | Landsvirkjun brand guidelines |

### Alternatives considered

* **ENTSO-E Transmission System Map:** authoritative, but there is no open GIS download and it is under ENTSO-E copyright. Useful only as a visual cross-check.
* **PyPSA-Eur prebuilt OSM network** (Zenodo, doi:10.5281/zenodo.18619025; Xiong et al., *Sci Data* 12, 277, 2025): well validated and ODbL, but covers only 220 kV and above, and not Iceland, Cyprus, Türkiye, Belarus or Russia.
* **GridKit** (2016): outdated.
* **Live Overpass API:** gives the same OSM data, but the public servers are often overloaded, which makes a Europe-wide query slow and unreliable.

## Known limitations

* **Only existing infrastructure is shown.** Planned and under-construction links are excluded by design.
* **Lines without a voltage tag are not drawn by default.** In 2026-10 this affected about 20–38% of line-km in Bosnia and Herzegovina, Montenegro, Kosovo, Luxembourg, Sweden, Switzerland and Norway, and 13% in Iceland, so these countries look somewhat sparser than they are. See `line_statistics_by_country.csv`.
* **National voltage levels differ.** France's sub-transmission grid is mostly 63/90 kV, below the threshold, so France looks less dense than Germany or Italy. The same applies, partly, to Spain (66 kV), Belgium (70 kV) and Iceland (66 kV).
* **OSM quality varies by country.** It is volunteer-mapped: coverage of 220 kV and above in Europe is near-complete, while 110–150 kV coverage is less complete in Russia, Belarus, Ukraine and Türkiye.
* **Some lines end at sea.** These are real offshore connections to oil/gas platforms and wind farms.
* **Lengths in the statistics are approximate**, because they are measured in EPSG:3035.
