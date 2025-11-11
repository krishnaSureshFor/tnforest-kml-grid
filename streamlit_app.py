import streamlit as st
import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.geometry import mapping
from pyproj import CRS
import math, os, tempfile, zipfile
from streamlit_folium import st_folium
import folium
from fpdf import FPDF
import matplotlib.pyplot as plt
import contextily as ctx
from lxml import etree
import fiona

# ================================================================
# APP CONFIG + THEME
# ================================================================
st.set_page_config(page_title="Forest Department — KML Grid Generator v4.3", layout="wide")

# 🌳 Custom gradient background and theme
st.markdown("""
<style>
.stApp { background: linear-gradient(135deg, #f9fbd7 0%, #e2f7ca 50%, #d2f5d7 100%); }
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #d9efff 0%, #bde0fe 100%);
    color: #1d3557;
}
section[data-testid="stSidebar"] h2 {
    color: #023047; font-weight: 800 !important; text-align: center;
    border-bottom: 2px solid #8ecae6; padding-bottom: 6px;
}
input, textarea, select {
    background-color: #fafff4 !important; border: 1px solid #b6d7a8 !important;
    color: #1b4332 !important; border-radius: 6px !important;
}
div.stButton > button {
    background: linear-gradient(90deg, #8fd694, #65c18c);
    color: white; font-weight: 600; border-radius: 10px; border: none;
    box-shadow: 1px 2px 5px rgba(0,0,0,0.2); transition: all 0.2s ease;
}
div.stButton > button:hover { background: linear-gradient(90deg, #79c781, #58b16e); transform: scale(1.03); }
.stDownloadButton > button {
    background: linear-gradient(90deg, #ffeb91, #ffd857);
    color: #333; border-radius: 10px; border: none; font-weight: 600;
    box-shadow: 1px 2px 4px rgba(0,0,0,0.15); transition: all 0.2s ease;
}
.stDownloadButton > button:hover { background: linear-gradient(90deg, #ffe372, #ffc94a); transform: scale(1.03); }
iframe[title="streamlit_folium"] {
    border-radius: 18px;
    border: 5px double transparent;
    background-image: linear-gradient(white, white), linear-gradient(90deg, #4caf50, #d4af37);
    background-origin: border-box; background-clip: content-box, border-box;
    box-shadow: 0 5px 12px rgba(0,0,0,0.25); padding: 2px;
}
</style>
""", unsafe_allow_html=True)

# ================================================================
# HEADER
# ================================================================
st.markdown("""
<div style='text-align:center; padding:15px; 
background:linear-gradient(90deg, #4caf50, #81c784);
border-radius:10px; color:white; font-size:28px; font-weight:700;
box-shadow:0 4px 10px rgba(0,0,0,0.25); letter-spacing:1px;'>
🌿 Forest Department — KML Grid Generator v4.3
</div>
""", unsafe_allow_html=True)

# ================================================================
# SIDEBAR
# ================================================================
st.sidebar.header("⚙️ Tool Settings")

with st.sidebar.expander("📂 Upload Files (AOI / Overlay)", expanded=True):
    uploaded_aoi = st.file_uploader("Upload AOI KML/KMZ", type=["kml", "kmz"], key="aoi_file")
    overlay_file = st.file_uploader("Optional Overlay KML/KMZ", type=["kml", "kmz"], key="overlay_file")


with st.sidebar.expander("🌲 KML Label Details"):
    range_name = st.text_input("Range Name", "Thammampatti", key="range_name")
    rf_name = st.text_input("RF Name", "Karumalai", key="rf_name")
    beat_name = st.text_input("Beat Name", "A1", key="beat_name")
    year_of_work = st.text_input("Year of Work", "2024", key="year_of_work")

with st.sidebar.expander("📄 PDF Report Details"):
    title_text = st.text_input("Report Title", "Removal of Invasive Species, Thammampatti Range", key="title_text")
    density = st.text_input("Density", "Medium", key="density")
    area_invasive = st.text_input("Area of Invasive (Ha)", "5", key="area_invasive")
    cell_size = st.number_input("Grid Cell Size (m)", 10, 2000, 100, 10, key="cell_size")
    generate_pdf = st.checkbox("Generate PDF Report", value=True, key="generate_pdf")

col1, col2 = st.sidebar.columns(2)
with col1: generate_click = st.button("▶ Generate Grid", key="btn_generate")
with col2: reset_click = st.button("🔄 Reset Map", key="btn_reset")

# ================================================================
# STATE
# ================================================================
def init_state():
    if "user_inputs" not in st.session_state:
        st.session_state["user_inputs"] = {
            "range_name": range_name, "rf_name": rf_name,
            "beat_name": beat_name, "year_of_work": year_of_work
        }
    if "generated" not in st.session_state:
        st.session_state["generated"] = False

init_state()

if reset_click:
    st.session_state.clear()
    init_state()
    st.rerun()
if generate_click:
    st.session_state["user_inputs"] = {
        "range_name": range_name, "rf_name": rf_name,
        "beat_name": beat_name, "year_of_work": year_of_work
    }
    st.session_state["generated"] = True

# ================================================================
# HELPERS
# ================================================================
def read_kml_safely(path):
    """Robustly read KML using Fiona fallback."""
    try:
        return gpd.read_file(path, driver="KML")
    except Exception:
        with fiona.Env():
            return gpd.read_file(path, engine="fiona", driver="KML")

def utm_crs_for_lonlat(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)

def make_grid_exact_clipped(polygons_ll, cell_size_m=100):
    merged_ll = unary_union(polygons_ll)
    centroid = merged_ll.centroid
    utm = utm_crs_for_lonlat(centroid.x, centroid.y)
    merged_utm = gpd.GeoSeries([merged_ll], crs=4326).to_crs(utm)
    minx, miny, maxx, maxy = merged_utm.total_bounds
    cols, rows = int(math.ceil((maxx - minx) / cell_size_m)), int(math.ceil((maxy - miny) / cell_size_m))
    cells = []
    aoi_union = merged_utm.unary_union
    for i in range(cols):
        for j in range(rows):
            x0, y0 = minx + i * cell_size_m, miny + j * cell_size_m
            cell = box(x0, y0, x0 + cell_size_m, y0 + cell_size_m)
            inter = cell.intersection(aoi_union)
            if not inter.is_empty:
                cells.append(inter)
    cells_ll = [gpd.GeoSeries([c], crs=utm).to_crs(4326).iloc[0] for c in cells]
    return cells_ll, merged_ll

# ================================================================
# KML GENERATORS (with Description + Balloon Popups)
# ================================================================
def _ring_coords_to_kml(ring):
    return " ".join(f"{pt[0]},{pt[1]},0" for pt in ring.coords if len(pt) >= 2)

def _write_polygon_coords(ns, parent_polygon_elem, geom):
    def write_one(poly):
        outer = etree.SubElement(parent_polygon_elem, "{%s}outerBoundaryIs" % ns)
        lr_out = etree.SubElement(outer, "{%s}LinearRing" % ns)
        etree.SubElement(lr_out, "{%s}coordinates" % ns).text = _ring_coords_to_kml(poly.exterior)
    if geom.geom_type == "Polygon":
        write_one(geom)
    elif geom.geom_type == "MultiPolygon":
        for part in geom.geoms:
            poly_elem = etree.SubElement(parent_polygon_elem.getparent(), "{%s}Polygon" % ns)
            write_one(part)

def _make_grid_balloon_text(user_inputs):
    return (
        "<![CDATA["
        "<b>Grid ID:</b> $[name]<br>"
        f"<b>Range:</b> {user_inputs['range_name']}<br>"
        f"<b>RF/RL:</b> {user_inputs['rf_name']}<br>"
        f"<b>Beat:</b> {user_inputs['beat_name']}<br>"
        f"<b>Year:</b> {user_inputs['year_of_work']}<br>"
        "<b>Area (Ha):</b> $[area_ha]<br>"
        "<hr><i>Developed by Krishna, Thammampatti Range</i>"
        "]]>"
    )

def generate_grid_only_kml(cells_ll, merged_ll, user_inputs):
    """Grid-only KML with same popup label as merged (no overlay)."""
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element("{%s}kml" % ns)
    doc = etree.SubElement(kml, "{%s}Document" % ns)

    etree.SubElement(doc, "{%s}name" % ns).text = "Grid Only"
    etree.SubElement(doc, "{%s}description" % ns).text = (
        "Grid-only file with labeled cells for field use. "
        "Developed by Krishna (Thammampatti Range)."
    )

    style_grid = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls1 = etree.SubElement(style_grid, "{%s}LineStyle" % ns)
    etree.SubElement(ls1, "{%s}color" % ns).text = "ff0000ff"  # red
    etree.SubElement(ls1, "{%s}width" % ns).text = "1"
    ps1 = etree.SubElement(style_grid, "{%s}PolyStyle" % ns)
    etree.SubElement(ps1, "{%s}fill" % ns).text = "0"
    balloon = etree.SubElement(style_grid, "{%s}BalloonStyle" % ns)
    etree.SubElement(balloon, "{%s}text" % ns).text = _make_grid_balloon_text(user_inputs)

    for i, cell in enumerate(cells_ll, 1):
        centroid = cell.centroid
        utm_crs = utm_crs_for_lonlat(centroid.x, centroid.y)
        area_ha = gpd.GeoSeries([cell], crs=4326).to_crs(utm_crs).area.iloc[0] / 10000.0

        pm = etree.SubElement(doc, "{%s}Placemark" % ns)
        etree.SubElement(pm, "{%s}name" % ns).text = str(i)
        etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"

        ext_data = etree.SubElement(pm, "{%s}ExtendedData" % ns)
        d = etree.SubElement(ext_data, "{%s}Data" % ns, name="area_ha")
        etree.SubElement(d, "{%s}value" % ns).text = f"{area_ha:.2f}"

        desc = etree.SubElement(pm, "{%s}description" % ns)
        desc.text = f"Grid {i} — Area: {area_ha:.2f} ha"

        poly = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly, cell)

    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

def generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf=None):
    """Labeled grid + overlay (gold) with popups and description."""
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element("{%s}kml" % ns)
    doc = etree.SubElement(kml, "{%s}Document" % ns)

    etree.SubElement(doc, "{%s}name" % ns).text = "Labeled Grid + Overlay"
    etree.SubElement(doc, "{%s}description" % ns).text = (
        "Labeled grid with overlay boundary. "
        "Developed by Krishna (Thammampatti Range)."
    )

    # Grid style
    style_grid = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls1 = etree.SubElement(style_grid, "{%s}LineStyle" % ns)
    etree.SubElement(ls1, "{%s}color" % ns).text = "ff0000ff"  # red
    etree.SubElement(ls1, "{%s}width" % ns).text = "1"
    ps1 = etree.SubElement(style_grid, "{%s}PolyStyle" % ns)
    etree.SubElement(ps1, "{%s}fill" % ns).text = "0"
    balloon = etree.SubElement(style_grid, "{%s}BalloonStyle" % ns)
    etree.SubElement(balloon, "{%s}text" % ns).text = _make_grid_balloon_text(user_inputs)

    # Overlay style (golden yellow 3px)
    style_overlay = etree.SubElement(doc, "{%s}Style" % ns, id="overlayStyle")
    ls2 = etree.SubElement(style_overlay, "{%s}LineStyle" % ns)
    etree.SubElement(ls2, "{%s}color" % ns).text = "ff00d7ff"  # ABGR for #FFD700
    etree.SubElement(ls2, "{%s}width" % ns).text = "3"
    ps2 = etree.SubElement(style_overlay, "{%s}PolyStyle" % ns)
    etree.SubElement(ps2, "{%s}fill" % ns).text = "0"

    # Grid placemarks
    for i, cell in enumerate(cells_ll, 1):
        centroid = cell.centroid
        utm_crs = utm_crs_for_lonlat(centroid.x, centroid.y)
        area_ha = gpd.GeoSeries([cell], crs=4326).to_crs(utm_crs).area.iloc[0] / 10000.0

        pm = etree.SubElement(doc, "{%s}Placemark" % ns)
        etree.SubElement(pm, "{%s}name" % ns).text = str(i)
        etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"

        ext_data = etree.SubElement(pm, "{%s}ExtendedData" % ns)
        d = etree.SubElement(ext_data, "{%s}Data" % ns, name="area_ha")
        etree.SubElement(d, "{%s}value" % ns).text = f"{area_ha:.2f}"

        desc = etree.SubElement(pm, "{%s}description" % ns)
        desc.text = f"Grid {i} — Area: {area_ha:.2f} ha"

        poly = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly, cell)

    # Overlay boundary
    if overlay_gdf is not None and not overlay_gdf.empty:
        og = overlay_gdf.to_crs(4326)
        for geom in og.geometry:
            if geom.is_empty:
                continue
            pm = etree.SubElement(doc, "{%s}Placemark" % ns)
            etree.SubElement(pm, "{%s}name" % ns).text = "Overlay Boundary"
            etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#overlayStyle"
            poly = etree.SubElement(pm, "{%s}Polygon" % ns)
            _write_polygon_coords(ns, poly, geom)

    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

# ================================================================
# PDF REPORT FUNCTION (stable layout + correct footer)
# ================================================================
def build_pdf_report_standard(
    cells_ll, merged_ll, user_inputs, cell_size,
    overlay_gdf, title_text, density, area_invasive
):
    import geopandas as gpd, matplotlib.pyplot as plt, contextily as ctx, tempfile, os
    from fpdf import FPDF

    MAP_X, MAP_Y, MAP_W, MAP_H, LEGEND_GAP = 15, 55, 180, 145, 8
    EMBLEM_PATH = os.path.join(os.path.dirname(__file__), "tn_emblem.png")

    class PDF(FPDF):
        def footer(self):
            self.set_y(-15)
            self.set_font("Helvetica", "I", 9)
            self.set_text_color(80, 80, 80)
            self.cell(0, 10, f"Page {self.page_no()}", 0, 0, "C")

    pdf = PDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=15)

    # Page 1 — Header
    pdf.add_page()
    if os.path.exists(EMBLEM_PATH):
        pdf.image(EMBLEM_PATH, x=93, y=8, w=25)
    pdf.set_y(35)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "FOREST DEPARTMENT", ln=1, align="C")
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, title_text, ln=1, align="C")

    # Map image
    tmp_dir = tempfile.gettempdir()
    map_img = os.path.join(tmp_dir, "map_overlay.png")
    fig, ax = plt.subplots(figsize=(7, 5.8))
    merged_gdf = gpd.GeoSeries([merged_ll], crs="EPSG:4326").to_crs(3857)
    grid_gdf = gpd.GeoSeries(cells_ll, crs="EPSG:4326").to_crs(3857)
    merged_gdf.boundary.plot(ax=ax, color="red", linewidth=3)            # AOI 3px red
    grid_gdf.boundary.plot(ax=ax, color="red", linewidth=1)              # Grid 1px red
    if overlay_gdf is not None and not overlay_gdf.empty:
        overlay_gdf.to_crs(3857).boundary.plot(ax=ax, color="#FFD700", linewidth=3)  # Overlay gold 3px
    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)  # do not pass attribution kw
    ax.axis("off"); plt.tight_layout(pad=0.1)
    fig.savefig(map_img, dpi=250, bbox_inches="tight"); plt.close(fig)
    pdf.image(map_img, x=MAP_X, y=MAP_Y, w=MAP_W, h=MAP_H)

    # Legend
    legend_y = MAP_Y + MAP_H + LEGEND_GAP
    pdf.set_y(legend_y)
    pdf.set_fill_color(245, 245, 240); pdf.set_draw_color(180, 180, 180)
    pdf.rect(MAP_X, legend_y, MAP_W, 40, style="FD")
    pdf.set_font("Helvetica", "", 11)
    col1 = [
        f"Range: {user_inputs['range_name']}",
        f"RF: {user_inputs['rf_name']}",
        f"Beat: {user_inputs['beat_name']}",
        f"Year: {user_inputs['year_of_work']}",
    ]
    col2 = [
        f"Density: {density}",
        f"Area of Invasive: {area_invasive} Ha",
        f"Cell Size: {cell_size} m",
        f"Overlay: {'Yes' if overlay_gdf is not None and not overlay_gdf.empty else 'No'}",
    ]
    for i in range(4):
        pdf.text(MAP_X + 10, legend_y + 10 + i * 6, col1[i])
        pdf.text(MAP_X + 100, legend_y + 10 + i * 6, col2[i])

    # Note
    pdf.set_y(legend_y + 47)
    pdf.set_font("Helvetica", "I", 9); pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 5, "Note: Satellite background (Esri) and boundaries are automatically generated. Developed by Rasipuram Range.")
    pdf.set_text_color(0, 0, 0)

    # Page 2 — Corner GPS Table (only if overlay exists)
    if overlay_gdf is not None and not overlay_gdf.empty:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 10, "Corner GPS of Overlay Area", ln=1, align="C")
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(25, 8, "S.No", 1, align="C")
        pdf.cell(75, 8, "Latitude", 1, align="C")
        pdf.cell(75, 8, "Longitude", 1, align="C"); pdf.ln(8)
        pdf.set_font("Helvetica", "", 10)

        row = 1
        overlay = overlay_gdf.to_crs(4326)
        for geom in overlay.geometry:
            if geom.is_empty:
                continue
            coords = []
            if geom.geom_type == "Polygon":
                coords = list(geom.exterior.coords)
            elif geom.geom_type == "MultiPolygon":
                for part in geom.geoms:
                    coords.extend(list(part.exterior.coords))
            for lon, lat, *_ in coords:
                pdf.cell(25, 7, str(row), 1)
                pdf.cell(75, 7, f"{lat:.6f}", 1, align="R")
                pdf.cell(75, 7, f"{lon:.6f}", 1, align="R")
                pdf.ln(7)
                row += 1
                if pdf.get_y() > 265:
                    pdf.add_page()
                    pdf.set_font("Helvetica", "B", 11)
                    pdf.cell(25, 8, "S.No", 1, align="C")
                    pdf.cell(75, 8, "Latitude", 1, align="C")
                    pdf.cell(75, 8, "Longitude", 1, align="C")
                    pdf.ln(8)
                    pdf.set_font("Helvetica", "", 10)

    # Output bytes
    result = pdf.output(dest="S")
    return bytes(result) if isinstance(result, (bytes, bytearray)) else result.encode("latin1", errors="ignore")
# ================================================================
# MAIN APP CONTROL FLOW — Runs only on Generate
# ================================================================

# 1️⃣ Collect user input values into session state on every render
# 1️⃣ Collect user input values (safe method — don't overwrite widget keys)
st.session_state["user_inputs"] = {
    "range_name": st.session_state.get("range_name", range_name),
    "rf_name": st.session_state.get("rf_name", rf_name),
    "beat_name": st.session_state.get("beat_name", beat_name),
    "year_of_work": st.session_state.get("year_of_work", year_of_work),
}

# Read widget state directly (no reassignment)
title_text = st.session_state.get("title_text", title_text)
density = st.session_state.get("density", density)
area_invasive = st.session_state.get("area_invasive", area_invasive)
cell_size = st.session_state.get("cell_size", cell_size)


# ================================================================
# CACHED OUTPUT GENERATOR
# ================================================================
@st.cache_data(show_spinner=False)
def generate_all_outputs(aoi_path, overlay_path, user_inputs, cell_size, title_text, density, area_invasive):
    gdf = read_kml_safely(aoi_path)
    polygons = gdf.geometry
    cells_ll, merged_ll = make_grid_exact_clipped(polygons, cell_size)

    overlay_gdf = None
    if overlay_path:
        overlay_gdf = read_kml_safely(overlay_path).to_crs(4326)

    grid_only_kml = generate_grid_only_kml(cells_ll, merged_ll, user_inputs)
    labeled_kml = generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf)
    pdf_bytes = build_pdf_report_standard(
        cells_ll, merged_ll, user_inputs, cell_size, overlay_gdf,
        title_text, density, area_invasive
    )

    return {
        "grid_only_kml": grid_only_kml,
        "labeled_kml": labeled_kml,
        "pdf_bytes": pdf_bytes,
        "overlay_gdf": overlay_gdf,
        "cells_ll": cells_ll,
        "merged_ll": merged_ll,
    }

# 2️⃣ Only execute heavy logic if user pressed Generate
if generate_click:
    st.session_state["generated"] = True

# 3️⃣ Only display map + outputs once generated
if st.session_state.get("generated", False):

    st.success("✅ Grid successfully generated! Scroll below to preview map and downloads.")
    aoi_path, ov_path = None, None

    # Handle AOI (required)
    if uploaded_aoi:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(uploaded_aoi.read())
            aoi_path = tmp.name
        if uploaded_aoi.name.lower().endswith(".kmz"):
            with zipfile.ZipFile(aoi_path) as z:
                kml = [f for f in z.namelist() if f.endswith(".kml")][0]
                aoi_path = os.path.join(tempfile.gettempdir(), "aoi.kml")
                with open(aoi_path, "wb") as f:
                    f.write(z.read(kml))
    else:
        st.warning("⚠️ Please upload an AOI file before generating.")
        st.stop()

    # Handle Overlay (optional)
    if overlay_file:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(overlay_file.read())
            ov_path = tmp.name
        if overlay_file.name.lower().endswith(".kmz"):
            with zipfile.ZipFile(ov_path) as z:
                kml = [f for f in z.namelist() if f.endswith(".kml")][0]
                ov_path = os.path.join(tempfile.gettempdir(), "overlay.kml")
                with open(ov_path, "wb") as f:
                    f.write(z.read(kml))

    # ============================================================
    # Run cached generator (no recomputation, no reload on download)
    # ============================================================
    outputs = generate_all_outputs(
        aoi_path, ov_path,
        st.session_state["user_inputs"],
        cell_size, title_text, density, area_invasive
    )
    for k, v in outputs.items():
        st.session_state[k] = v

    # ============================================================
    # MAP PREVIEW — Static and stable
    # ============================================================
    m = folium.Map(location=[11, 78.5], zoom_start=8)

    gdf_for_bounds = read_kml_safely(aoi_path)
    aoi_union = unary_union(gdf_for_bounds.geometry)

    # AOI boundary
    folium.GeoJson(
        mapping(aoi_union),
        style_function=lambda x: {"color": "red", "weight": 3, "fillOpacity": 0}
    ).add_to(m)

    # Grid cells
    for c in st.session_state["cells_ll"]:
        folium.GeoJson(
            mapping(c),
            style_function=lambda x: {"color": "red", "weight": 1, "fillOpacity": 0}
        ).add_to(m)

    # Overlay
    if st.session_state["overlay_gdf"] is not None and not st.session_state["overlay_gdf"].empty:
        for g in st.session_state["overlay_gdf"].geometry:
            if g.is_empty:
                continue
            folium.GeoJson(
                mapping(g),
                style_function=lambda x: {"color": "#FFD700", "weight": 3, "fillOpacity": 0}
            ).add_to(m)

    # Fit bounds and display
    bounds = [
        [aoi_union.bounds[1], aoi_union.bounds[0]],
        [aoi_union.bounds[3], aoi_union.bounds[2]],
    ]
    m.fit_bounds(bounds)
    st_folium(m, width=1200, height=700)

    # ============================================================
    # DOWNLOADS — No reload on click
    # ============================================================
    st.markdown("### 💾 Downloads")
    c1, c2, c3 = st.columns(3)

    with c1:
        st.download_button(
            "📦 Download Grid Only KML",
            st.session_state["grid_only_kml"],
            file_name="grid_only.kml",
            mime="application/vnd.google-earth.kml+xml",
        )
    with c2:
        st.download_button(
            "🧾 Download Labeled + Overlay KML",
            st.session_state["labeled_kml"],
            file_name="merged_labeled.kml",
            mime="application/vnd.google-earth.kml+xml",
        )
    with c3:
        if generate_pdf:
            st.download_button(
                "📄 Download Invasive Report (PDF)",
                st.session_state["pdf_bytes"],
                file_name="Invasive_Report.pdf",
                mime="application/pdf",
            )

else:
    st.info("👆 Upload AOI (KML/KMZ) and Overlay, adjust details, then click ▶ **Generate Grid**.")

# Optional: Hide Streamlit spinner for smoother UI
st.markdown("<style>.stSpinner{display:none}</style>", unsafe_allow_html=True)


