import os
import io
import math
import tempfile
from datetime import datetime

import streamlit as st
import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.geometry import mapping
from pyproj import CRS
from lxml import etree
from streamlit_folium import st_folium
import folium

# NEW: map drawing deps
import contextily as ctx
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
import requests

from fpdf import FPDF

# ------------------------------------------------------------------------------
# Streamlit setup
# ------------------------------------------------------------------------------
st.set_page_config(page_title="KML to Grid Generator v3.0", layout="wide")
st.title("🗺️ KML to Grid Generator v3.0")

# ------------------------------------------------------------------------------
# State
# ------------------------------------------------------------------------------
def init_state():
    if "user_inputs" not in st.session_state:
        st.session_state["user_inputs"] = {
            "range_name": "",
            "rf_name": "",
            "beat_name": "",
            "year_of_work": ""
        }
    if "generated" not in st.session_state:
        st.session_state["generated"] = False

init_state()

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def utm_crs_for_lonlat(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)

def make_grid_exact_clipped(polygons_ll, cell_size_m=100):
    merged_ll = unary_union(polygons_ll)
    centroid = merged_ll.centroid
    utm = utm_crs_for_lonlat(centroid.x, centroid.y)

    merged_utm = gpd.GeoSeries([merged_ll], crs="EPSG:4326").to_crs(utm)
    minx, miny, maxx, maxy = merged_utm.total_bounds

    cols = int(math.ceil((maxx - minx) / cell_size_m))
    rows = int(math.ceil((maxy - miny) / cell_size_m))

    cells = []
    aoi_union = merged_utm.unary_union  # (Deprecation warning is fine)
    for i in range(cols):
        for j in range(rows):
            x0 = minx + i * cell_size_m
            y0 = miny + j * cell_size_m
            cell = box(x0, y0, x0 + cell_size_m, y0 + cell_size_m)
            if aoi_union.intersects(cell):
                inter = cell.intersection(aoi_union)
                if not inter.is_empty:
                    cells.append(inter)

    # back to WGS84
    cells_ll = [gpd.GeoSeries([geom], crs=utm).to_crs(4326).iloc[0] for geom in cells]
    return cells_ll, merged_ll

# ------------------ Robust KML coordinate writer ------------------
def _ring_coords_to_kml(ring):
    coords_list = []
    for pt in ring.coords:
        if len(pt) >= 2:
            coords_list.append(f"{pt[0]},{pt[1]},0")
    return " ".join(coords_list)

def _write_polygon_coords(ns, parent_polygon_elem, geom):
    def write_one(poly):
        outer = etree.SubElement(parent_polygon_elem, "{%s}outerBoundaryIs" % ns)
        lr_out = etree.SubElement(outer, "{%s}LinearRing" % ns)
        etree.SubElement(lr_out, "{%s}coordinates" % ns).text = _ring_coords_to_kml(poly.exterior)
        for hole in getattr(poly, "interiors", []):
            inner = etree.SubElement(parent_polygon_elem, "{%s}innerBoundaryIs" % ns)
            lr_in = etree.SubElement(inner, "{%s}LinearRing" % ns)
            etree.SubElement(lr_in, "{%s}coordinates" % ns).text = _ring_coords_to_kml(hole)

    if geom.geom_type == "Polygon":
        write_one(geom)
    elif geom.geom_type == "MultiPolygon":
        for part in geom.geoms:
            poly_elem = etree.SubElement(parent_polygon_elem.getparent(), "{%s}Polygon" % ns)
            write_one(part)

# ----------------- KML generators -----------------
def generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf=None):
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element("{%s}kml" % ns)
    doc = etree.SubElement(kml, "{%s}Document" % ns)
    etree.SubElement(doc, "{%s}name" % ns).text = "Labeled Grid"
    etree.SubElement(doc, "{%s}description" % ns).text = "Developed by Rasipuram Range"

    style_grid = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls1 = etree.SubElement(style_grid, "{%s}LineStyle" % ns)
    etree.SubElement(ls1, "{%s}color" % ns).text = "ff0000ff"  # red
    etree.SubElement(ls1, "{%s}width" % ns).text = "1"
    ps1 = etree.SubElement(style_grid, "{%s}PolyStyle" % ns)
    etree.SubElement(ps1, "{%s}fill" % ns).text = "0"

    if overlay_gdf is not None and not overlay_gdf.empty:
        style_ov = etree.SubElement(doc, "{%s}Style" % ns, id="overlayStyle")
        ls2 = etree.SubElement(style_ov, "{%s}LineStyle" % ns)
        etree.SubElement(ls2, "{%s}color" % ns).text = "ff00d7ff"  # golden-ish
        etree.SubElement(ls2, "{%s}width" % ns).text = "3"
        ps2 = etree.SubElement(style_ov, "{%s}PolyStyle" % ns)
        etree.SubElement(ps2, "{%s}fill" % ns).text = "0"

    for i, cell in enumerate(cells_ll, start=1):
        pm = etree.SubElement(doc, "{%s}Placemark" % ns)
        etree.SubElement(pm, "{%s}name" % ns).text = f"{i}"
        etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"
        poly_elem = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly_elem, cell)

    if overlay_gdf is not None and not overlay_gdf.empty:
        if overlay_gdf.crs is None:
            overlay_gdf = overlay_gdf.set_crs(4326)
        else:
            overlay_gdf = overlay_gdf.to_crs(4326)
        for geom in overlay_gdf.geometry:
            if geom.is_empty:
                continue
            pm = etree.SubElement(doc, "{%s}Placemark" % ns)
            etree.SubElement(pm, "{%s}name" % ns).text = "Overlay"
            etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#overlayStyle"
            poly_elem = etree.SubElement(pm, "{%s}Polygon" % ns)
            _write_polygon_coords(ns, poly_elem, geom)

    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

def generate_grid_only_kml(cells_ll, merged_ll):
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element("{%s}kml" % ns)
    doc = etree.SubElement(kml, "{%s}Document" % ns)
    etree.SubElement(doc, "{%s}name" % ns).text = "Grid Only"
    etree.SubElement(doc, "{%s}description" % ns).text = "Generated Grid"

    style = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls = etree.SubElement(style, "{%s}LineStyle" % ns)
    etree.SubElement(ls, "{%s}color" % ns).text = "ff0000ff"
    etree.SubElement(ls, "{%s}width" % ns).text = "1"
    ps = etree.SubElement(style, "{%s}PolyStyle" % ns)
    etree.SubElement(ps, "{%s}fill" % ns).text = "0"

    for i, cell in enumerate(cells_ll, start=1):
        pm = etree.SubElement(doc, "{%s}Placemark" % ns)
        etree.SubElement(pm, "{%s}name" % ns).text = f"{i}"
        etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"
        poly = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly, cell)

    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

# ------------------------------------------------------------------------------
# PDF REPORT (satellite bg, exact top view, north arrow, legend 2-col, header)
# ------------------------------------------------------------------------------
FONT_PATH = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
EMBLEM_PATH = os.path.join(os.path.dirname(__file__), "tn_emblem.png")

def _ensure_font():
    if os.path.exists(FONT_PATH):
        return True
    try:
        url = "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans.ttf"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        with open(FONT_PATH, "wb") as f:
            f.write(r.content)
        return True
    except Exception:
        return False

def _ensure_emblem():
    """Try to ensure a valid PNG emblem exists locally. If fail, skip silently."""
    if os.path.exists(EMBLEM_PATH):
        try:
            Image.open(EMBLEM_PATH).verify()
            return True
        except Exception:
            pass
    try:
        # small transparent PNG fallback (TN emblem from public source)
        url = "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1f/Tamil_Nadu_Emblem.svg/240px-Tamil_Nadu_Emblem.svg.png"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        with open(EMBLEM_PATH, "wb") as f:
            f.write(r.content)
        Image.open(EMBLEM_PATH).verify()
        return True
    except Exception:
        return False

def _draw_topview_map_png(merged_ll, cells_ll, overlay_gdf):
    """Render a top-view map to PNG using Web Mercator with satellite basemap.
       Keeps image size bounded to avoid gigantic PNGs."""
    # Project geometries to Web Mercator
    aoi_4326 = gpd.GeoSeries([merged_ll], crs=4326)
    aoi_3857 = aoi_4326.to_crs(3857)
    bounds = aoi_3857.total_bounds  # xmin, ymin, xmax, ymax
    xmin, ymin, xmax, ymax = bounds

    # Pad bounds ~3%
    pad_x = (xmax - xmin) * 0.03
    pad_y = (ymax - ymin) * 0.03
    xmin -= pad_x; xmax += pad_x; ymin -= pad_y; ymax += pad_y

    # Prepare GeoDataFrames in 3857
    grid_gdf = gpd.GeoDataFrame(geometry=cells_ll, crs=4326).to_crs(3857)
    ov_gdf = None
    if overlay_gdf is not None and not overlay_gdf.empty:
        ov_gdf = overlay_gdf.copy()
        ov_gdf = (ov_gdf.set_crs(4326) if ov_gdf.crs is None else ov_gdf.to_crs(4326)).to_crs(3857)

    # Figure sizing cap: ~1500x1000 px
    fig_w, fig_h, dpi = 7.5, 5.0, 200  # 1500x1000
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)

    # Set extent and aspect
    ax.set_xlim([xmin, xmax])
    ax.set_ylim([ymin, ymax])
    ax.set_aspect("equal")

    # Basemap (satellite)
    try:
        ctx.add_basemap(ax, crs="EPSG:3857", source=ctx.providers.Esri.WorldImagery, attribution_size=5)
    except Exception:
        # fallback: light basemap if Esri blocked
        ctx.add_basemap(ax, crs="EPSG:3857", source=ctx.providers.OpenTopoMap, attribution_size=5)

    # AOI outline — red, 3px
    aoi_3857.boundary.plot(ax=ax, color="#FF0000", linewidth=3, zorder=30)

    # Grid — red, 1px thin
    if not grid_gdf.empty:
        grid_gdf.boundary.plot(ax=ax, color="#FF0000", linewidth=1, zorder=20)

    # Overlay — golden yellow, 3px
    if ov_gdf is not None and not ov_gdf.empty:
        ov_gdf.boundary.plot(ax=ax, color="#FFD700", linewidth=3, zorder=40)

    # North arrow (simple)
    # Draw a small arrow at top-left
    ax.annotate("N", xy=(0.06, 0.88), xytext=(0.06, 0.97),
                xycoords="axes fraction", textcoords="axes fraction",
                arrowprops=dict(arrowstyle="-|>", linewidth=2),
                fontsize=10, ha="center")

    # Remove axes
    ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    plt.tight_layout(pad=0)
    img_path = os.path.join(tempfile.gettempdir(), "map_topview.png")
    plt.savefig(img_path, bbox_inches="tight", pad_inches=0, dpi=dpi)
    plt.close(fig)
    return img_path

def build_pdf_report_standard(cells_ll, merged_ll, overlay_gdf, user_inputs,
                              cell_size, overlay_present, title_text, density, area_invasive):
    # prepare assets
    have_font = _ensure_font()
    _ensure_emblem()

    # --- PAGE 1 (Map + Legend) ---
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    # Fonts
    if have_font:
        pdf.add_font("DejaVu", "", FONT_PATH, uni=True)
        pdf.add_font("DejaVu", "B", FONT_PATH, uni=True)
        pdf.add_font("DejaVu", "I", FONT_PATH, uni=True)
        def fnt(name="DejaVu", style="", size=11): pdf.set_font(name, style, size)
    else:
        def fnt(name="Helvetica", style="", size=11): pdf.set_font(name, style, size)

    # Header: FOREST | Emblem | DEPARTMENT
    fnt(size=18, style="B")
    pdf.cell(65, 10, "FOREST", align="R")
    pdf.cell(60, 10, "", align="C")
    pdf.cell(65, 10, "DEPARTMENT", align="L")
    pdf.ln(10)
    if os.path.exists(EMBLEM_PATH):
        try:
            pdf.image(EMBLEM_PATH, x=90, y=8, w=30, h=30)
        except Exception:
            pass
    pdf.set_y(28)
    pdf.set_draw_color(0, 100, 0)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(6)

    # Title (from user)
    fnt(style="B", size=14)
    pdf.cell(0, 8, title_text, align="C")
    pdf.ln(8)

    # Map image (top view exact)
    map_png = _draw_topview_map_png(merged_ll, cells_ll, overlay_gdf)
    # keep margins: x=15, w=180 leaves room for legend under
    pdf.image(map_png, x=15, y=pdf.get_y(), w=180)
    pdf.ln(100)  # move below image

    # Legend box label
    # put subtle background
    pdf.set_fill_color(245, 245, 245)
    pdf.set_draw_color(200, 200, 200)
    pdf.rect(15, pdf.get_y(), 180, 30, style="FD")
    pdf.set_xy(15, pdf.get_y() + 2)
    fnt(style="B", size=12)
    pdf.cell(0, 6, "Legend")

    # two-column legend rows
    fnt(size=10)
    rows_left = [
        f"Range: {user_inputs.get('range_name','')}",
        f"RF: {user_inputs.get('rf_name','')}",
        f"Beat: {user_inputs.get('beat_name','')}",
        f"Year of Work: {user_inputs.get('year_of_work','')}",
    ]
    rows_right = [
        f"Density: {density}",
        f"Area of Invasive: {area_invasive} Ha",
        f"Grid Size: {cell_size} m",
        f"Overlay Included: {'Yes' if overlay_present else 'No'}",
    ]

    start_y = pdf.get_y() + 8
    pdf.set_xy(20, start_y)
    row_h = 5.0
    for r in rows_left:
        pdf.cell(85, row_h, r)
        pdf.ln(row_h)

    pdf.set_xy(110, start_y)
    for r in rows_right:
        pdf.cell(85, row_h, r)
        pdf.ln(row_h)

    pdf.ln(4)
    # Color key under legend
    fnt(style="B", size=10)
    pdf.cell(0, 6, "Color Key")
    fnt(size=10)
    y = pdf.get_y() + 2
    # Grid (red 1px)
    pdf.set_draw_color(255, 0, 0); pdf.set_line_width(0.4)
    pdf.line(20, y, 50, y); pdf.set_xy(55, y - 2); pdf.cell(0, 5, "Grid (Red, 1 px)")
    y += 6
    # AOI (red 3px)
    pdf.set_line_width(1.2); pdf.line(20, y, 50, y); pdf.set_xy(55, y - 2); pdf.cell(0, 5, "AOI (Red, 3 px)")
    y += 6
    # Overlay (golden 3px)
    pdf.set_draw_color(255, 215, 0); pdf.set_line_width(1.2); pdf.line(20, y, 50, y)
    pdf.set_xy(55, y - 2); pdf.cell(0, 5, "Overlay (Golden Yellow, 3 px)")

    # Footer note at very bottom
    pdf.set_y(-20)
    fnt(style="I", size=9)
    pdf.multi_cell(0, 5, "Developed by Rasipuram Range")

    # Page number
    pdf.set_y(-12)
    fnt(size=9)
    pdf.cell(0, 5, f"Page 1", align="R")

    # --- Convert to bytes
    out = pdf.output(dest="S")  # returns bytes/bytearray/str depending on version
    if isinstance(out, bytearray):
        out = bytes(out)
    elif isinstance(out, str):
        out = out.encode("latin1", errors="ignore")
    return out

# ------------------------------------------------------------------------------
# Sidebar UI
# ------------------------------------------------------------------------------
st.sidebar.header("⚙️ Options")
uploaded_aoi = st.sidebar.file_uploader("Upload AOI KML/KMZ", type=["kml", "kmz"])
overlay_file = st.sidebar.file_uploader("Optional Overlay KML/KMZ", type=["kml", "kmz"])
cell_size = st.sidebar.number_input("Grid cell size (meters)", min_value=10, max_value=2000, value=100, step=10)

range_name = st.sidebar.text_input("Range Name", "Thammampatty")
rf_name = st.sidebar.text_input("RF Name", "Paithur RF")
beat_name = st.sidebar.text_input("Beat Name", "Paithur South")
year_of_work = st.sidebar.text_input("Year of Work", "2024")

title_text = st.sidebar.text_input("🧭 Report Title", "Removal of Invasive Species — Thammampatty Range")
density = st.sidebar.text_input("Density", "Medium")
area_invasive = st.sidebar.text_input("Area of Invasive (Ha)", "5")

if st.sidebar.button("➕ Add Input Labels"):
    st.session_state["user_inputs"] = {
        "range_name": range_name,
        "rf_name": rf_name,
        "beat_name": beat_name,
        "year_of_work": year_of_work
    }
    st.sidebar.success("✅ Label inputs added.")

generate_pdf = st.sidebar.checkbox("📄 Generate PDF Report", value=True)

col_btn1, col_btn2 = st.sidebar.columns(2)
with col_btn1:
    generate_click = st.button("▶ Generate Grid")
with col_btn2:
    reset_click = st.button("🔄 Reset Map")

if reset_click:
    st.session_state.clear()
    init_state()
    st.experimental_rerun()

if generate_click:
    st.session_state["generated"] = True

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
if st.session_state["generated"]:
    m = folium.Map(location=[11.0, 78.5], zoom_start=8)
    bounds = None
    overlay_gdf = None
    cells_ll = []
    merged_ll = None

    if uploaded_aoi is not None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".kml") as tmp:
            tmp.write(uploaded_aoi.read())
            tmp_path = tmp.name
        gdf = gpd.read_file(tmp_path, driver="KML")
        polygons = gdf.geometry
        cells_ll, merged_ll = make_grid_exact_clipped(polygons, cell_size)

        aoi_union = unary_union(polygons)
        # AOI outline (red, thicker)
        folium.GeoJson(
            mapping(aoi_union),
            name="AOI",
            style_function=lambda x: {"color": "red", "weight": 3, "fillOpacity": 0}
        ).add_to(m)

        # Grid (red thin)
        for cell in cells_ll:
            folium.GeoJson(
                mapping(cell),
                name="Grid",
                style_function=lambda x: {"color": "red", "weight": 1, "fillOpacity": 0}
            ).add_to(m)

        minx, miny, maxx, maxy = aoi_union.bounds
        bounds = [[miny, minx], [maxy, maxx]]

    if overlay_file is not None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".kml") as tmp2:
            tmp2.write(overlay_file.read())
            tmp2_path = tmp2.name
        overlay_gdf = gpd.read_file(tmp2_path, driver="KML")
        for geom in overlay_gdf.geometry:
            if geom.is_empty:
                continue
            folium.GeoJson(
                mapping(geom),
                name="Overlay",
                style_function=lambda x: {"color": "#FFD700", "weight": 3, "fillOpacity": 0}
            ).add_to(m)

        if bounds is None and not overlay_gdf.empty:
            minx, miny, maxx, maxy = overlay_gdf.total_bounds
            bounds = [[miny, minx], [maxy, maxx]]

    if bounds:
        m.fit_bounds(bounds)

    st_folium(m, width=1200, height=700)

    if uploaded_aoi is not None:
        user_inputs = st.session_state["user_inputs"]
        grid_count = len(cells_ll)
        total_area_ha = sum([c.area * (111000 ** 2) / 10000 for c in cells_ll])
        st.success(f"✅ Generated {grid_count} grid cells covering approximately {total_area_ha:.2f} ha inside AOI")

        # Both KMLs
        grid_only_kml = generate_grid_only_kml(cells_ll, merged_ll)
        labeled_kml = generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf)

        st.markdown("### 💾 Downloads")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.download_button(
                "📦 Download Grid Only KML",
                grid_only_kml,
                file_name="grid_only.kml",
                mime="application/vnd.google-earth.kml+xml"
            )
        with col2:
            st.download_button(
                "🧾 Download Labeled + Merged KML",
                labeled_kml,
                file_name="grid_labeled.kml",
                mime="application/vnd.google-earth.kml+xml"
            )
        with col3:
            if generate_pdf:
                pdf_bytes = build_pdf_report_standard(
                    cells_ll, merged_ll, overlay_gdf,
                    user_inputs, cell_size, overlay_file is not None,
                    title_text, density, area_invasive
                )
                st.download_button(
                    "📄 Download Report (PDF)",
                    pdf_bytes,
                    file_name="grid_report.pdf",
                    mime="application/pdf"
                )
else:
    st.info("👆 Upload AOI (and optional Overlay), add labels, then press **▶ Generate Grid**.")
