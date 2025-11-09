import streamlit as st
import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union
from pyproj import CRS
import math, os, tempfile, requests
from shapely.geometry import mapping
from streamlit_folium import st_folium
import folium
from fpdf import FPDF
from datetime import datetime

# ================================================================
# 🧩 BASIC SETUP
# ================================================================
st.set_page_config(page_title="KML to Grid Generator v4.2", layout="wide")
st.title("🗺️ KML to Grid Generator v4.2")

def init_state():
    if "user_inputs" not in st.session_state:
        st.session_state["user_inputs"] = {
            "range_name": "", "rf_name": "", "beat_name": "", "year_of_work": ""
        }
    if "generated" not in st.session_state:
        st.session_state["generated"] = False
init_state()

# ================================================================
# 🔧 HELPERS
# ================================================================
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

def _ring_coords_to_kml(ring):
    return " ".join(f"{pt[0]},{pt[1]},0" for pt in ring.coords if len(pt) >= 2)

def _write_polygon_coords(ns, parent_polygon_elem, geom):
    from lxml import etree
    def write_one(poly):
        outer = etree.SubElement(parent_polygon_elem, "{%s}outerBoundaryIs" % ns)
        lr_out = etree.SubElement(outer, "{%s}LinearRing" % ns)
        etree.SubElement(lr_out, "{%s}coordinates" % ns).text = _ring_coords_to_kml(poly.exterior)
        for hole in getattr(poly, "interiors", []):
            inner = etree.SubElement(parent_polygon_elem, "{%s}innerBoundaryIs" % ns)
            lr_in = etree.SubElement(inner, "{%s}LinearRing" % ns)
            etree.SubElement(lr_in, "{%s}coordinates" % ns).text = _ring_coords_to_kml(hole)
    from lxml import etree
    if geom.geom_type == "Polygon":
        write_one(geom)
    elif geom.geom_type == "MultiPolygon":
        for part in geom.geoms:
            poly_elem = etree.SubElement(parent_polygon_elem.getparent(), "{%s}Polygon" % ns)
            write_one(part)

# ================================================================
# 📄 PDF REPORT GENERATOR (2 pages)
# ================================================================
def build_pdf_report_standard(
    cells_ll, merged_ll, user_inputs, cell_size,
    overlay_present, title_text, density, area_invasive
):
    import tempfile
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=12)

    # 🔰 Tamil Nadu emblem (safe load)
    EMBLEM_PATH = os.path.join(os.path.dirname(__file__), "tn_emblem.png")

    def safe_image(pdf, path, x, y, w, h):
        """Safely load an image without breaking the app."""
        if os.path.exists(path):
            try:
                pdf.image(path, x=x, y=y, w=w, h=h)
            except Exception as e:
                print(f"⚠️ Emblem skipped due to error: {e}")
        else:
            print("⚠️ Emblem image not found, skipping...")

    # ==================== PAGE 1 ====================
    pdf.add_page()
    pdf.set_fill_color(0, 100, 0)
    pdf.rect(0, 0, 210, 20, "F")

    # Header section
    safe_image(pdf, EMBLEM_PATH, x=95, y=2, w=20, h=20)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 14)
    pdf.text(25, 14, "FOREST")
    pdf.text(160, 14, "DEPARTMENT")

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 14)
    pdf.ln(22)
    pdf.cell(0, 8, title_text, ln=1, align="C")

    # Satellite map download
    centroid = merged_ll.centroid
    minx, miny, maxx, maxy = merged_ll.bounds
    center_lon, center_lat = (minx + maxx) / 2, (miny + maxy) / 2
    sat_url = f"https://static-maps.yandex.ru/1.x/?lang=en_US&ll={center_lon},{center_lat}&z=14&l=sat&size=650,450"
    tmp_dir = tempfile.gettempdir()
    map_img = os.path.join(tmp_dir, "aoi_map.png")

    try:
        r = requests.get(sat_url, timeout=15)
        with open(map_img, "wb") as f:
            f.write(r.content)
    except Exception:
        map_img = None

    if map_img and os.path.exists(map_img):
        pdf.image(map_img, x=15, y=40, w=180)
    pdf.set_y(140)

    # Legend below map (2 columns)
    pdf.set_font("Helvetica", "", 11)
    col1 = [
        f"Range: {user_inputs.get('range_name','')}",
        f"RF: {user_inputs.get('rf_name','')}",
        f"Beat: {user_inputs.get('beat_name','')}",
        f"Year of Work: {user_inputs.get('year_of_work','')}"
    ]
    col2 = [
        f"Density: {density}",
        f"Area of Invasive: {area_invasive} Ha",
        f"Cell Size: {cell_size} m",
        f"Overlay Included: {'Yes' if overlay_present is not None and not overlay_present.empty else 'No'}"
    ]

    y = pdf.get_y() + 5
    for i in range(4):
        pdf.text(20, y + i * 6, col1[i])
        pdf.text(110, y + i * 6, col2[i])

    pdf.set_y(175)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "Developed by Rasipuram Range")

    pdf.set_y(-15)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 10, f"Page {pdf.page_no()} / 2", 0, 0, "C")

    # ==================== PAGE 2 ====================
    pdf.add_page()
    pdf.set_fill_color(0, 100, 0)
    pdf.rect(0, 0, 210, 20, "F")
    safe_image(pdf, EMBLEM_PATH, x=95, y=2, w=20, h=20)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 14)
    pdf.text(25, 14, "FOREST")
    pdf.text(160, 14, "DEPARTMENT")

    pdf.set_text_color(0, 0, 0)
    pdf.ln(20)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Invasive Area Field Report", ln=1, align="C")

    # Table 1: Corner GPS
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Corner GPS of Invasive Area", ln=1)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(20, 8, "Sl.No", 1)
    pdf.cell(60, 8, "Latitude", 1)
    pdf.cell(60, 8, "Longitude", 1)
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 10)
    if overlay_present is not None:
        idx = 1
        for geom in overlay_present.geometry:
            if geom.geom_type == "Polygon":
                for coord in geom.exterior.coords:
                    if len(coord) >= 2:
                        x, y = coord[0], coord[1]
                        pdf.cell(20, 7, str(idx), 1)
                        pdf.cell(60, 7, f"{y:.6f}", 1)
                        pdf.cell(60, 7, f"{x:.6f}", 1)
                        pdf.ln(7)
                        idx += 1

    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Invasive Grid Area Details", ln=1)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(20, 8, "Sl.No", 1)
    pdf.cell(30, 8, "Grid ID", 1)
    pdf.cell(35, 8, "Area (ha)", 1)
    pdf.cell(45, 8, "Latitude", 1)
    pdf.cell(45, 8, "Longitude", 1)
    pdf.ln(8)

    total_area = 0
    pdf.set_font("Helvetica", "", 10)
    for i, geom in enumerate(cells_ll, start=1):
        lat, lon = geom.centroid.y, geom.centroid.x
        area_ha = geom.area * (111000 ** 2) / 10000
        total_area += area_ha
        pdf.cell(20, 7, str(i), 1)
        pdf.cell(30, 7, f"G{i}", 1)
        pdf.cell(35, 7, f"{area_ha:.2f}", 1)
        pdf.cell(45, 7, f"{lat:.5f}", 1)
        pdf.cell(45, 7, f"{lon:.5f}", 1)
        pdf.ln(7)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(50, 7, "TOTAL", 1)
    pdf.cell(35, 7, f"{total_area:.2f}", 1)
    pdf.cell(90, 7, "", 1)
    pdf.ln(10)

    pdf.set_y(-15)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 10, f"Page {pdf.page_no()} / 2", 0, 0, "C")

    result = pdf.output(dest="S").encode("latin1", errors="ignore")
    return result
# ================================================================
# 🧰 STREAMLIT SIDEBAR
# ================================================================
st.sidebar.header("⚙️ Options")
uploaded_aoi = st.sidebar.file_uploader("Upload AOI KML/KMZ", type=["kml", "kmz"])
overlay_file = st.sidebar.file_uploader("Optional Overlay KML/KMZ", type=["kml", "kmz"])
cell_size = st.sidebar.number_input("Grid cell size (meters)", 10, 2000, 100, 10)
range_name = st.sidebar.text_input("Range Name", "Thammampatti")
rf_name = st.sidebar.text_input("RF Name", "Karumalai")
beat_name = st.sidebar.text_input("Beat Name", "A1")
year_of_work = st.sidebar.text_input("Year of Work", "2024")
title_text = st.sidebar.text_input("🧭 Report Title", "Removal of Invasive Species, Thammampatti Range")
density = st.sidebar.text_input("Density", "Medium")
area_invasive = st.sidebar.text_input("Area of Invasive (Ha)", "5")

if st.sidebar.button("➕ Add Input Labels"):
    st.session_state["user_inputs"] = {
        "range_name": range_name, "rf_name": rf_name,
        "beat_name": beat_name, "year_of_work": year_of_work
    }
    st.sidebar.success("✅ Label inputs added.")

generate_pdf = st.sidebar.checkbox("📄 Generate PDF Report", value=True)
col1, col2 = st.sidebar.columns(2)
with col1: generate_click = st.button("▶ Generate Grid")
with col2: reset_click = st.button("🔄 Reset Map")

if reset_click:
    st.session_state.clear()
    init_state()
    st.experimental_rerun()
if generate_click:
    st.session_state["generated"] = True

# ================================================================
# 🗺️ MAIN UI
# ================================================================
if st.session_state["generated"]:
    m = folium.Map(location=[11.0, 78.5], zoom_start=8)
    bounds, overlay_gdf = None, None
    cells_ll, merged_ll = [], None

    if uploaded_aoi is not None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".kml") as tmp:
            tmp.write(uploaded_aoi.read())
            tmp_path = tmp.name
        gdf = gpd.read_file(tmp_path, driver="KML")
        polygons = gdf.geometry
        cells_ll, merged_ll = make_grid_exact_clipped(polygons, cell_size)
        aoi_union = unary_union(polygons)
        folium.GeoJson(mapping(aoi_union),
            name="AOI", style_function=lambda x: {"color": "red", "weight": 3, "fillOpacity": 0}).add_to(m)
        for cell in cells_ll:
            folium.GeoJson(mapping(cell),
                name="Grid", style_function=lambda x: {"color": "red", "weight": 1, "fillOpacity": 0}).add_to(m)
        minx, miny, maxx, maxy = aoi_union.bounds
        bounds = [[miny, minx], [maxy, maxx]]

    if overlay_file is not None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".kml") as tmp2:
            tmp2.write(overlay_file.read())
            tmp2_path = tmp2.name
        overlay_gdf = gpd.read_file(tmp2_path, driver="KML")
        for geom in overlay_gdf.geometry:
            if not geom.is_empty:
                folium.GeoJson(mapping(geom),
                    name="Overlay", style_function=lambda x: {"color": "#FFD700", "weight": 3, "fillOpacity": 0}).add_to(m)
        if bounds is None and not overlay_gdf.empty:
            minx, miny, maxx, maxy = overlay_gdf.total_bounds
            bounds = [[miny, minx], [maxy, maxx]]

    if bounds:
        m.fit_bounds(bounds)
    st_folium(m, width=1200, height=700)

    if uploaded_aoi is not None:
        user_inputs = st.session_state["user_inputs"]
        grid_kml = "<kml dummy>"  # simplified here
        st.markdown("### 💾 Downloads")
        col1, col2 = st.columns(2)
        with col1:
            st.download_button("📦 Download Grid Only KML",
                grid_kml, file_name="grid_only.kml",
                mime="application/vnd.google-earth.kml+xml")
        with col2:
            if generate_pdf:
                pdf_bytes = build_pdf_report_standard(
                    cells_ll, merged_ll, user_inputs, cell_size,
                    overlay_gdf, title_text, density, area_invasive)
                st.download_button("📄 Download Invasive Report (PDF)",
                    pdf_bytes, file_name="Invasive_Report.pdf", mime="application/pdf")
else:
    st.info("👆 Upload AOI, add labels, then click ▶ Generate Grid.")



