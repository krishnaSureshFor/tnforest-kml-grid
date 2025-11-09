import streamlit as st
import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union
from pyproj import CRS
import math, os, tempfile, io, requests
from lxml import etree
from shapely.geometry import mapping
from streamlit_folium import st_folium
import folium
from fpdf import FPDF
from datetime import datetime
import matplotlib.pyplot as plt
import contextily as ctx
import matplotlib.patches as patches
import numpy as np


# ----------------------------------------------------------------------
st.set_page_config(page_title="KML Grid Generator v3.1", layout="wide")
st.title("🗺️ KML to Grid Generator v3.1 — Rasipuram Range")

# ----------------------------------------------------------------------
def init_state():
    if "user_inputs" not in st.session_state:
        st.session_state["user_inputs"] = {
            "range_name": "", "rf_name": "", "beat_name": "", "year_of_work": ""
        }
    if "generated" not in st.session_state:
        st.session_state["generated"] = False
init_state()

# ----------------------------------------------------------------------
def utm_crs_for_lonlat(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)

# ----------------------------------------------------------------------
def make_grid_exact_clipped(polygons_ll, cell_size_m=100):
    merged_ll = unary_union(polygons_ll)
    centroid = merged_ll.centroid
    utm = utm_crs_for_lonlat(centroid.x, centroid.y)

    merged_utm = gpd.GeoSeries([merged_ll], crs="EPSG:4326").to_crs(utm)
    minx, miny, maxx, maxy = merged_utm.total_bounds
    cols = int(math.ceil((maxx - minx) / cell_size_m))
    rows = int(math.ceil((maxy - miny) / cell_size_m))

    cells = []
    aoi_union = merged_utm.unary_union
    for i in range(cols):
        for j in range(rows):
            x0 = minx + i * cell_size_m
            y0 = miny + j * cell_size_m
            cell = box(x0, y0, x0 + cell_size_m, y0 + cell_size_m)
            if aoi_union.intersects(cell):
                inter = cell.intersection(aoi_union)
                if not inter.is_empty:
                    cells.append(inter)
    cells_ll = [gpd.GeoSeries([geom], crs=utm).to_crs(4326).iloc[0] for geom in cells]
    return cells_ll, merged_ll

# ----------------------------------------------------------------------
def _ring_coords_to_kml(ring):
    return " ".join([f"{pt[0]},{pt[1]},0" for pt in ring.coords if len(pt) >= 2])

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

# ----------------------------------------------------------------------
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

def generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf=None):
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element("{%s}kml" % ns)
    doc = etree.SubElement(kml, "{%s}Document" % ns)
    etree.SubElement(doc, "{%s}name" % ns).text = "Labeled Grid"
    etree.SubElement(doc, "{%s}description" % ns).text = "Developed by Rasipuram Range"

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
        poly_elem = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly_elem, cell)
    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

# ----------------------------------------------------------------------
# 🧾 PDF REPORT WITH TRUE MAP OVERLAY
# ----------------------------------------------------------------------
def build_pdf_report_standard(cells_ll, merged_ll, overlay_gdf, user_inputs,
                              cell_size, overlay_present, title_text, density, area_invasive):
    # --- Prepare geometries ---
    aoi_gdf = gpd.GeoSeries([merged_ll], crs="EPSG:4326").to_crs(3857)
    grid_gdf = gpd.GeoDataFrame(geometry=cells_ll, crs="EPSG:4326").to_crs(3857)
    overlay_gdf = overlay_gdf.to_crs(3857) if overlay_gdf is not None else None

    # === MAP RENDERING (Top View with North Arrow + Adaptive Scale Bar) ===
    fig, ax = plt.subplots(figsize=(8, 4.8))
    ctx.add_basemap(ax, source=ctx.providers.Esri.WorldImagery, zoom=13, attribution=False)
    aoi_gdf.boundary.plot(ax=ax, color="#FF0000", linewidth=3)
    grid_gdf.boundary.plot(ax=ax, color="#FF0000", linewidth=1)
    if overlay_gdf is not None and not overlay_gdf.empty:
        overlay_gdf.boundary.plot(ax=ax, color="#FFD700", linewidth=3)
    ax.set_axis_off()

    # --- North Arrow ---
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    circle_center = (xlim[1] - (xlim[1]-xlim[0])*0.07, ylim[1] - (ylim[1]-ylim[0])*0.1)
    radius = (xlim[1]-xlim[0])*0.015
    ax.add_patch(patches.Circle(circle_center, radius, edgecolor="black", facecolor="#2E8B57", lw=2, zorder=5))
    ax.annotate("", xy=(circle_center[0], circle_center[1]+radius*0.6),
                xytext=(circle_center[0], circle_center[1]-radius*0.4),
                arrowprops=dict(facecolor="white", edgecolor="white", width=3, headwidth=8), zorder=6)
    ax.text(circle_center[0], circle_center[1]-radius*0.8, "N", ha="center", va="center",
            fontsize=12, fontweight="bold", color="white", zorder=6)

    # --- Adaptive Scale Bar ---
    width_m = (xlim[1] - xlim[0])
    if width_m < 3000:
        scalebar_length_m, label = 100, "100 m"
    elif width_m < 10000:
        scalebar_length_m, label = 500, "500 m"
    else:
        scalebar_length_m, label = 1000, "1 km"

    m_per_pixel = width_m / ax.get_window_extent().width
    bar_length_screen = scalebar_length_m / m_per_pixel
    bar_x_start = xlim[0] + (xlim[1]-xlim[0])*0.05
    bar_y = ylim[0] + (ylim[1]-ylim[0])*0.05
    bar_x_end = bar_x_start + bar_length_screen

    # Alternating black/white segments
    segment_count = 4
    segment_len = (bar_x_end - bar_x_start) / segment_count
    for i in range(segment_count):
        color = "white" if i % 2 == 0 else "black"
        ax.plot([bar_x_start + i*segment_len, bar_x_start + (i+1)*segment_len],
                [bar_y, bar_y], color=color, linewidth=5, solid_capstyle="butt")
    ax.text((bar_x_start+bar_x_end)/2, bar_y - 25*m_per_pixel, label,
            ha="center", va="top", fontsize=10, color="#2E8B57", fontweight="bold")

    plt.tight_layout(pad=0)
    img_path = os.path.join(tempfile.gettempdir(), "map_topview.png")
    plt.savefig(img_path, bbox_inches="tight", pad_inches=0, dpi=250)
    plt.close(fig)

    # === PDF SETUP ===
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=12)

    font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
    emblem_path = os.path.join(os.path.dirname(__file__), "tn_emblem.png")

    if os.path.exists(font_path):
        pdf.add_font("DejaVu","",font_path,uni=True)
        pdf.add_font("DejaVu","B",font_path,uni=True)
        pdf.add_font("DejaVu","I",font_path,uni=True)

    # --- Header Bar (Repeated) ---
    def add_header():
        pdf.set_fill_color(0, 100, 0)
        pdf.rect(0, 0, 210, 20, style="F")
        pdf.set_y(4)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 18)
        pdf.cell(65, 10, "FOREST", align="R")
        pdf.cell(60, 10, "", align="C")
        pdf.cell(65, 10, "DEPARTMENT", ln=1, align="L")
        if os.path.exists(emblem_path):
            pdf.image(emblem_path, x=90, y=3, w=30, h=30)
        pdf.set_y(26)
        pdf.set_draw_color(0, 100, 0)
        pdf.line(15, pdf.get_y(), 195, pdf.get_y())
        pdf.ln(8)
        pdf.set_text_color(0, 0, 0)

    # === PAGE 1 : Map + Legend ===
    pdf.add_page()
    add_header()
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 14)
    pdf.cell(0, 8, title_text, ln=1, align="C")
    pdf.ln(4)

    pdf.image(img_path, x=15, y=pdf.get_y(), w=180, h=95)
    pdf.rect(15, pdf.get_y(), 180, 95)
    pdf.ln(100)

    # --- Legend Box ---
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(0, 100, 0)
    start_y = pdf.get_y()
    pdf.rect(15, start_y, 180, 50)
    pdf.set_xy(20, start_y + 4)

    pdf.set_text_color(0, 100, 0)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 12)
    pdf.cell(0, 7, "Legend", ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "", 11)

    left_data = [
        f"Range: {user_inputs.get('range_name','')}",
        f"Beat: {user_inputs.get('beat_name','')}",
        f"Grid Size: {cell_size} m",
        f"Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}"
    ]
    right_data = [
        f"RF: {user_inputs.get('rf_name','')}",
        f"Density: {density}",
        f"Area of Invasive: {area_invasive} Ha",
        f"Overlay: {'Yes' if overlay_present else 'No'}"
    ]
    for l, r in zip(left_data, right_data):
        pdf.cell(85, 6, l, border=0)
        pdf.cell(85, 6, r, ln=1, border=0)
    pdf.ln(2)

    pdf.set_text_color(0, 100, 0)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 11)
    pdf.cell(0, 6, "Color Key", ln=1)
    pdf.set_text_color(0, 0, 0)
    legend_items = [
        ("AOI Boundary", "#FF0000", 3),
        ("Grid (1 Ha)", "#FF0000", 1),
        ("Overlay (Cleared Area)", "#FFD700", 3)
    ]
    for label, color, thickness in legend_items:
        r, g, b = [int(color[i:i+2], 16) for i in (1,3,5)]
        pdf.set_draw_color(r, g, b)
        y = pdf.get_y() + 3
        pdf.line(25, y, 50, y)
        pdf.set_xy(55, pdf.get_y())
        pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "", 10)
        pdf.cell(0, 6, label, ln=1)

    pdf.set_y(-18)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "Developed by Rasipuram Range", align="C")

    # === PAGE 2 : Tables ===
    pdf.add_page()
    add_header()
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 14)
    pdf.cell(0, 8, title_text, ln=1, align="C")
    pdf.ln(10)

    # Summary in two columns
    summary_left = [
        f"Range: {user_inputs.get('range_name','')}",
        f"Beat: {user_inputs.get('beat_name','')}",
        f"RF: {user_inputs.get('rf_name','')}"
    ]
    summary_right = [
        f"Year of Work: {user_inputs.get('year_of_work','')}",
        f"Area of Invasive: {area_invasive} Ha",
        "Each Grid = 1 Ha"
    ]
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "", 12)
    for l, r in zip(summary_left, summary_right):
        pdf.cell(95, 8, l, border=0)
        pdf.cell(95, 8, r, ln=1, border=0)
    pdf.ln(8)

    # Corner GPS Table
    pdf.set_font("DejaVu", "B", 13)
    pdf.cell(0, 8, "Corner GPS of Invasive Area", ln=1, align="C")
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(25, 8, "SL No", 1, 0, "C")
    pdf.cell(60, 8, "Latitude", 1, 0, "C")
    pdf.cell(60, 8, "Longitude", 1, 1, "C")
    pdf.set_font("DejaVu", "", 10)

    if overlay_gdf is not None and not overlay_gdf.empty:
        idx = 1
        for geom in overlay_gdf.to_crs(4326).geometry:
            if geom.geom_type == "Polygon":
                for (x, y) in list(geom.exterior.coords)[:-1]:
                    pdf.cell(25, 6, str(idx), 1, 0, "C")
                    pdf.cell(60, 6, f"{y:.6f}", 1, 0, "C")
                    pdf.cell(60, 6, f"{x:.6f}", 1, 1, "C")
                    idx += 1
    else:
        pdf.cell(0, 8, "No overlay polygons available.", 1, 1, "C")
    pdf.ln(10)

    # Grid Table
    pdf.set_font("DejaVu", "B", 13)
    pdf.cell(0, 8, "Invasive Grid Area Details", ln=1, align="C")
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(20, 8, "SL No", 1, 0, "C")
    pdf.cell(40, 8, "Grid ID", 1, 0, "C")
    pdf.cell(40, 8, "Area (Ha)", 1, 0, "C")
    pdf.cell(45, 8, "Latitude", 1, 0, "C")
    pdf.cell(45, 8, "Longitude", 1, 1, "C")
    pdf.set_font("DejaVu", "", 10)

    total_area = 0
    for i, geom in enumerate(cells_ll, start=1):
        centroid = geom.centroid
        utm = gpd.GeoSeries([geom], crs=4326).estimate_utm_crs()
        area_ha = float(gpd.GeoSeries([geom], crs=4326).to_crs(utm).area.iloc[0]) / 10000
        total_area += area_ha
        pdf.cell(20, 6, str(i), 1, 0, "C")
        pdf.cell(40, 6, f"Grid-{i}", 1, 0, "C")
        pdf.cell(40, 6, f"{area_ha:.2f}", 1, 0, "R")
        pdf.cell(45, 6, f"{geom.centroid.y:.6f}", 1, 0, "R")
        pdf.cell(45, 6, f"{geom.centroid.x:.6f}", 1, 1, "R")

    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(60, 8, "TOTAL", 1, 0, "C")
    pdf.cell(40, 8, f"{total_area:.2f}", 1, 0, "R")
    pdf.cell(90, 8, "", 1, 1, "C")

    # Page numbers
    num_pages = len(pdf.pages)
    for i in range(num_pages):
        pdf.page = i + 1
        pdf.set_y(-10)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(0, 8, f"Page {i + 1} of {num_pages}", align="R")

    result = pdf.output(dest="S")
    if isinstance(result, bytearray):
        result = bytes(result)
    elif isinstance(result, str):
        result = result.encode("latin1", errors="ignore")
    return result
    # ✅ (The rest of your second page with GPS and table remains unchanged)
# ----------------------------------------------------------------------
# 🧰 SIDEBAR UI
# ----------------------------------------------------------------------
st.sidebar.header("⚙️ Options")

uploaded_aoi = st.sidebar.file_uploader("Upload AOI KML/KMZ", type=["kml", "kmz"])
overlay_file = st.sidebar.file_uploader("Optional Overlay KML/KMZ", type=["kml", "kmz"])
cell_size = st.sidebar.number_input("Grid Cell Size (m)", 10, 2000, 100, 10)

range_name = st.sidebar.text_input("Range Name", "Thammampatty")
rf_name = st.sidebar.text_input("RF Name", "Paithur RF")
beat_name = st.sidebar.text_input("Beat Name", "Paithur South")
year_of_work = st.sidebar.text_input("Year of Work", "2024")
title_text = st.sidebar.text_input("🧭 Report Title", "Removal of Invasive Species, Thammampatty Range")
density = st.sidebar.text_input("Density", "Medium")
area_invasive = st.sidebar.text_input("Area of Invasive (Ha)", "5")

if st.sidebar.button("➕ Add Input Labels"):
    st.session_state["user_inputs"] = {
        "range_name": range_name, "rf_name": rf_name, "beat_name": beat_name, "year_of_work": year_of_work
    }
    st.sidebar.success("✅ Labels saved.")

generate_pdf = st.sidebar.checkbox("📄 Generate PDF Report", value=True)
col_btn1, col_btn2 = st.sidebar.columns(2)
with col_btn1:
    generate_click = st.button("▶ Generate Grid")
with col_btn2:
    reset_click = st.button("🔄 Reset")

if reset_click:
    st.session_state.clear()
    init_state()
    st.experimental_rerun()

if generate_click:
    st.session_state["generated"] = True

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
if st.session_state["generated"]:
    m = folium.Map(location=[11.0, 78.5], zoom_start=8)
    bounds = None
    overlay_gdf = None
    cells_ll = []
    merged_ll = None

    if uploaded_aoi:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".kml") as tmp:
            tmp.write(uploaded_aoi.read()); tmp_path = tmp.name
        gdf = gpd.read_file(tmp_path, driver="KML")
        polygons = gdf.geometry
        cells_ll, merged_ll = make_grid_exact_clipped(polygons, cell_size)
        aoi_union = unary_union(polygons)
        folium.GeoJson(mapping(aoi_union), name="AOI",
                       style_function=lambda x: {"color": "red", "weight": 1}).add_to(m)
        for cell in cells_ll:
            folium.GeoJson(mapping(cell), name="Grid",
                           style_function=lambda x: {"color": "yellow", "weight": 1}).add_to(m)
        minx, miny, maxx, maxy = aoi_union.bounds
        bounds = [[miny, minx], [maxy, maxx]]

    if overlay_file:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".kml") as tmp2:
            tmp2.write(overlay_file.read()); tmp2_path = tmp2.name
        overlay_gdf = gpd.read_file(tmp2_path, driver="KML")
        for geom in overlay_gdf.geometry:
            if not geom.is_empty:
                folium.GeoJson(mapping(geom), name="Overlay",
                               style_function=lambda x: {"color": "#FFD700", "weight": 3}).add_to(m)
        if bounds is None and not overlay_gdf.empty:
            minx, miny, maxx, maxy = overlay_gdf.total_bounds
            bounds = [[miny, minx], [maxy, maxx]]

    if bounds:
        m.fit_bounds(bounds)

    st_folium(m, width=1200, height=700)

    if uploaded_aoi:
        user_inputs = st.session_state["user_inputs"]
        grid_kml = generate_grid_only_kml(cells_ll, merged_ll)
        labeled_kml = generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf)
        st.markdown("### 💾 Downloads")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.download_button("📦 Download Grid Only (KML)", grid_kml,
                               file_name="grid_only.kml", mime="application/vnd.google-earth.kml+xml")
        with col2:
            st.download_button("🧾 Download Grid + Labels (KML)", labeled_kml,
                               file_name="grid_labeled.kml", mime="application/vnd.google-earth.kml+xml")
        with col3:
            if generate_pdf:
                pdf_bytes = build_pdf_report_standard(
                    cells_ll, merged_ll, overlay_gdf, user_inputs, cell_size,
                    overlay_file is not None, title_text, density, area_invasive
                )
                st.download_button("📄 Download Report (PDF)", pdf_bytes,
                                   file_name="grid_report.pdf", mime="application/pdf")
else:
    st.info("👆 Upload AOI, set labels, then press ▶ Generate Grid.")





