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
    centroid = merged_ll.centroid
    aoi_gdf = gpd.GeoSeries([merged_ll], crs="EPSG:4326").to_crs(3857)
    grid_gdf = gpd.GeoDataFrame(geometry=cells_ll, crs="EPSG:4326").to_crs(3857)
    overlay_gdf = overlay_gdf.to_crs(3857) if overlay_gdf is not None else None

    # --- Render map ---
    fig, ax = plt.subplots(figsize=(8, 4.8))
    aoi_gdf.boundary.plot(ax=ax, color="#FF0000", linewidth=3)
    grid_gdf.boundary.plot(ax=ax, color="#FF0000", linewidth=1)
    if overlay_gdf is not None and not overlay_gdf.empty:
        overlay_gdf.boundary.plot(ax=ax, color="#FFD700", linewidth=3)
    ctx.add_basemap(ax, source=ctx.providers.Esri.WorldImagery, zoom=12, attribution=False)
    ax.set_axis_off()

    # 🧭 Tamil-style North Compass (circle + arrow)
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    circle_center = (xlim[1] - (xlim[1] - xlim[0]) * 0.07, ylim[1] - (ylim[1] - ylim[0]) * 0.1)
    radius = (xlim[1] - xlim[0]) * 0.015

    circle = patches.Circle(circle_center, radius, edgecolor="black", facecolor="#2E8B57", lw=2, zorder=5)
    ax.add_patch(circle)

    # Upward arrow inside circle
    ax.annotate(
        "", xy=(circle_center[0], circle_center[1] + radius * 0.6),
        xytext=(circle_center[0], circle_center[1] - radius * 0.4),
        arrowprops=dict(facecolor="white", edgecolor="white", shrink=0.05, width=3, headwidth=8),
        zorder=6
    )

    # "N" label below arrow
    ax.text(circle_center[0], circle_center[1] - radius * 0.8, "N",
            ha="center", va="center", fontsize=12, fontweight="bold", color="white", zorder=6)

    plt.tight_layout(pad=0)
    img_path = os.path.join(tempfile.gettempdir(), "final_map.png")
    plt.savefig(img_path, bbox_inches="tight", pad_inches=0, dpi=250)
    plt.close(fig)

    # --- PDF ---
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    # 🟩 Page border
    pdf.set_draw_color(0, 100, 0)
    pdf.set_line_width(0.7)
    pdf.rect(5, 5, 200, 287)

    # === Fonts ===
    font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
    if os.path.exists(font_path):
        pdf.add_font("DejaVu", "", font_path, uni=True)
        pdf.add_font("DejaVu", "B", font_path, uni=True)
        pdf.add_font("DejaVu", "I", font_path, uni=True)
        pdf.set_font("DejaVu", "B", 18)
    else:
        pdf.set_font("Helvetica", "B", 18)

    # === Header ===
    emblem_path = os.path.join(os.path.dirname(__file__), "tn_emblem.png")
    header_y = 10
    pdf.set_text_color(0, 100, 0)
    pdf.cell(65, 10, "FOREST", align="R")
    pdf.cell(60, 10, "", align="C")
    pdf.cell(65, 10, "DEPARTMENT", ln=1, align="L")
    pdf.set_text_color(0, 0, 0)
    if os.path.exists(emblem_path):
        pdf.image(emblem_path, x=90, y=header_y - 2, w=30, h=30)
    pdf.set_y(header_y + 20)
    pdf.set_draw_color(0, 100, 0)
    pdf.set_line_width(0.5)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(6)

    # === Title ===
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 14)
    pdf.cell(0, 8, title_text, ln=1, align="C")
    pdf.ln(4)

    # === Map ===
    map_y = pdf.get_y()
    pdf.image(img_path, x=15, y=map_y, w=180, h=95)
    pdf.set_draw_color(0, 100, 0)
    pdf.set_line_width(0.5)
    pdf.rect(15, map_y, 180, 95)
    pdf.set_y(map_y + 102)

    # === Legend ===
    pdf.set_fill_color(245, 245, 245)
    pdf.set_draw_color(200, 200, 200)
    pdf.set_line_width(0.2)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 12)
    pdf.cell(0, 8, "Legend", ln=1, align="L", fill=True)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "", 11)
    pdf.ln(2)

    col_width = 95
    row_height = 6
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
        pdf.cell(col_width, row_height, l, border=0)
        pdf.cell(col_width, row_height, r, ln=1, border=0)
    pdf.ln(5)

    # === Color Key ===
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 11)
    pdf.cell(0, 7, "Color Key", ln=1)
    pdf.ln(1)
    legend_items = [
        ("AOI Boundary", "#FF0000", 3),
        ("Grid (1 Ha)", "#FF0000", 1),
        ("Overlay (Cleared Area)", "#FFD700", 3)
    ]
    for label, color, thickness in legend_items:
        r, g, b = tuple(int(color[i:i+2], 16) for i in (1, 3, 5))
        pdf.set_draw_color(r, g, b)
        y = pdf.get_y() + 3
        pdf.line(20, y, 50, y)
        pdf.set_xy(55, pdf.get_y())
        pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "", 10)
        pdf.cell(0, 6, label, ln=1)
    pdf.ln(4)

    # === Footer ===
    pdf.set_y(-18)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "I", 9)
    pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(0, 5, "Developed by Rasipuram Range", align="C")

    # Return PDF
    result = pdf.output(dest="S")
    if isinstance(result, bytearray):
        result = bytes(result)
    elif isinstance(result, str):
        result = result.encode("latin1", errors="ignore")
    return result
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




