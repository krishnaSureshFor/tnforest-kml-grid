import streamlit as st
import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.geometry import mapping
from pyproj import CRS
import math, os, tempfile, zipfile, io
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
# HEADER BANNER
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
# SIDEBAR LAYOUT
# ================================================================
st.sidebar.header("⚙️ Tool Settings")

with st.sidebar.expander("📂 Upload Files (AOI / Overlay)", expanded=True):
    uploaded_aoi = st.file_uploader("Upload AOI KML/KMZ", type=["kml", "kmz"])
    overlay_file = st.file_uploader("Optional Overlay KML/KMZ", type=["kml", "kmz"])

with st.sidebar.expander("🌲 KML Label Details"):
    range_name = st.text_input("Range Name", placeholder="Enter Range Name")
    rf_name = st.text_input("RF Name", placeholder="Enter RF/RL")
    beat_name = st.text_input("Beat Name", placeholder="Enter Beat Name")
    year_of_work = st.text_input("Year of Work", placeholder="Enter Year")

with st.sidebar.expander("📄 PDF Report Details"):
    title_text = st.text_input("Report Title", placeholder="Title with Range Name")
    density = st.text_input("Density", placeholder="Light/Medium/High")
    area_invasive = st.text_input("Area of Invasive (Ha)", placeholder="5 ha")
    cell_size = st.number_input("Grid Cell Size (m)", 10, 2000, 100, 10)
    generate_pdf = st.checkbox("Generate PDF Report", value=True)

col1, col2 = st.sidebar.columns(2)
with col1: generate_click = st.button("▶ Generate Grid")
with col2: reset_click = st.button("🔄 Reset Map")

# ================================================================
# STATE INIT
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
    st.session_state["generated"] = True

# ================================================================
# HELPERS
# ================================================================
def read_kml_safely(path):
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
    for i in range(cols):
        for j in range(rows):
            x0, y0 = minx + i * cell_size_m, miny + j * cell_size_m
            cell = box(x0, y0, x0 + cell_size_m, y0 + cell_size_m)
            inter = cell.intersection(merged_utm.unary_union)
            if not inter.is_empty:
                cells.append(inter)
    return [gpd.GeoSeries([c], crs=utm).to_crs(4326).iloc[0] for c in cells], merged_ll

# ================================================================
# KML GENERATORS
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

def generate_grid_only_kml(cells_ll, merged_ll):
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element("{%s}kml" % ns)
    doc = etree.SubElement(kml, "{%s}Document" % ns)
    etree.SubElement(doc, "{%s}name" % ns).text = "Grid Only"
    style = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls = etree.SubElement(style, "{%s}LineStyle" % ns)
    etree.SubElement(ls, "{%s}color" % ns).text = "ff0000ff"
    etree.SubElement(ls, "{%s}width" % ns).text = "1"
    ps = etree.SubElement(style, "{%s}PolyStyle" % ns)
    etree.SubElement(ps, "{%s}fill" % ns).text = "0"
    for i, cell in enumerate(cells_ll, 1):
        pm = etree.SubElement(doc, "{%s}Placemark" % ns)
        etree.SubElement(pm, "{%s}name" % ns).text = str(i)
        etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"
        poly = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly, cell)
    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

def generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf=None):
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element("{%s}kml" % ns)
    doc = etree.SubElement(kml, "{%s}Document" % ns)
    etree.SubElement(doc, "{%s}name" % ns).text = "Labeled Grid + Overlay"
    etree.SubElement(doc, "{%s}description" % ns).text = (
        "Generated by Forest Department — Developed by Krishna."
    )
    # Style and popup logic same as before
    style_grid = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls1 = etree.SubElement(style_grid, "{%s}LineStyle" % ns)
    etree.SubElement(ls1, "{%s}color" % ns).text = "ff0000ff"
    etree.SubElement(ls1, "{%s}width" % ns).text = "1"
    ps1 = etree.SubElement(style_grid, "{%s}PolyStyle" % ns)
    etree.SubElement(ps1, "{%s}fill" % ns).text = "0"

    for i, cell in enumerate(cells_ll, 1):
        pm = etree.SubElement(doc, "{%s}Placemark" % ns)
        etree.SubElement(pm, "{%s}name" % ns).text = str(i)
        etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"
        desc = etree.SubElement(pm, "{%s}description" % ns)
        desc.text = f"Grid {i} - {user_inputs['beat_name']}"
        poly = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly, cell)

    if overlay_gdf is not None and not overlay_gdf.empty:
        og = overlay_gdf.to_crs(4326)
        for geom in og.geometry:
            if geom.is_empty: continue
            pm = etree.SubElement(doc, "{%s}Placemark" % ns)
            etree.SubElement(pm, "{%s}name" % ns).text = "Overlay"
            etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"
            poly = etree.SubElement(pm, "{%s}Polygon" % ns)
            _write_polygon_coords(ns, poly, geom)

    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

# ================================================================
# PDF REPORT FUNCTION
# ================================================================
def build_pdf_report_standard(cells_ll, merged_ll, user_inputs, cell_size, overlay_gdf, title_text, density, area_invasive):
    pdf = FPDF("P", "mm", "A4")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "FOREST DEPARTMENT", ln=1, align="C")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, title_text, ln=1, align="C")
    pdf.ln(5)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, f"Range: {user_inputs['range_name']}  |  Beat: {user_inputs['beat_name']}", ln=1)
    pdf.cell(0, 6, f"RF: {user_inputs['rf_name']}  |  Year: {user_inputs['year_of_work']}", ln=1)
    pdf.cell(0, 6, f"Density: {density}  |  Area of Invasive: {area_invasive} Ha", ln=1)
    pdf.cell(0, 6, f"Cell Size: {cell_size} m", ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "Note: Satellite background and boundaries are auto-generated. Developed by Rasipuram Range.")
    pdf_output = io.BytesIO()
    pdf.output(pdf_output)
    return pdf_output.getvalue()

# ================================================================
# MAIN LOGIC
# ================================================================
if st.session_state["generated"]:
    m = folium.Map(location=[11, 78.5], zoom_start=8)
    bounds, overlay_gdf, cells_ll, merged_ll = None, None, [], None

    # Handle AOI
    if uploaded_aoi:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(uploaded_aoi.read())
            tmp_path = tmp.name
        if uploaded_aoi.name.lower().endswith(".kmz"):
            with zipfile.ZipFile(tmp_path, "r") as z:
                kml_files = [f for f in z.namelist() if f.endswith(".kml")]
                extracted = os.path.join(tempfile.gettempdir(), "aoi.kml")
                with open(extracted, "wb") as f:
                    f.write(z.read(kml_files[0]))
            tmp_path = extracted
        gdf = read_kml_safely(tmp_path)
        polygons = gdf.geometry
        cells_ll, merged_ll = make_grid_exact_clipped(polygons, cell_size)
        aoi_union = unary_union(polygons)
        folium.GeoJson(mapping(aoi_union),
                       style_function=lambda x: {"color": "red", "weight": 3}).add_to(m)
        for c in cells_ll:
            folium.GeoJson(mapping(c),
                           style_function=lambda x: {"color": "red", "weight": 1}).add_to(m)
        bounds = [[aoi_union.bounds[1], aoi_union.bounds[0]],
                  [aoi_union.bounds[3], aoi_union.bounds[2]]]

    # Overlay
    if overlay_file:
        with tempfile.NamedTemporaryFile(delete=False) as tmp2:
            tmp2.write(overlay_file.read())
            tmp2_path = tmp2.name
        if overlay_file.name.lower().endswith(".kmz"):
            with zipfile.ZipFile(tmp2_path, "r") as z:
                kml_files = [f for f in z.namelist() if f.endswith(".kml")]
                extracted_overlay = os.path.join(tempfile.gettempdir(), "overlay.kml")
                with open(extracted_overlay, "wb") as f:
                    f.write(z.read(kml_files[0]))
            tmp2_path = extracted_overlay
        overlay_gdf = read_kml_safely(tmp2_path).to_crs(4326)
        for geom in overlay_gdf.geometry:
            if geom.is_empty: continue
            folium.GeoJson(mapping(geom),
                           style_function=lambda x: {"color": "#FFD700", "weight": 3}).add_to(m)

    if bounds:
        m.fit_bounds(bounds)
    st_folium(m, width=1200, height=700)

    # Downloads
    user_inputs = st.session_state["user_inputs"]
    grid_only_kml = generate_grid_only_kml(cells_ll, merged_ll)
    labeled_kml = generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf)
    pdf_bytes = build_pdf_report_standard(cells_ll, merged_ll, user_inputs, cell_size, overlay_gdf, title_text, density, area_invasive)

    st.markdown("### 💾 Downloads")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("📦 Download Grid Only KML", grid_only_kml, file_name="grid_only.kml")
    with c2:
        st.download_button("🧾 Download Labeled + Overlay KML", labeled_kml, file_name="merged_labeled.kml")
    with c3:
        st.download_button("📄 Download Invasive Report (PDF)", pdf_bytes, file_name="Invasive_Report.pdf", mime="application/pdf")
else:
    st.info("👆 Upload AOI (KML/KMZ), optionally Overlay, then click ▶ Generate Grid.")
