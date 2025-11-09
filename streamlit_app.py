import streamlit as st
import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union
from pyproj import CRS
import math
from lxml import etree
from shapely.geometry import mapping
from streamlit_folium import st_folium
import folium
import tempfile
from fpdf import FPDF
from datetime import datetime
import os

# -------------------------------------------------------------
# 🌍 APP CONFIG
# -------------------------------------------------------------
st.set_page_config(page_title="KML to Grid Generator v2.0", layout="wide")
st.title("🗺️ KML to Grid Generator v2.0")

# ------------------------- Utils / State -------------------------
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

# ---------------------- CRS helper -------------------
def utm_crs_for_lonlat(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)

# --------------- Grid generator (clipped AOI) --------------
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

# ------------------ Robust KML coordinate writer ------------------
def _ring_coords_to_kml(ring):
    return " ".join([f"{pt[0]},{pt[1]},0" for pt in ring.coords if len(pt) >= 2])

def _write_polygon_coords(ns, parent_polygon_elem, geom):
    def write_one(poly):
        outer = etree.SubElement(parent_polygon_elem, f"{{{ns}}}outerBoundaryIs")
        lr_out = etree.SubElement(outer, f"{{{ns}}}LinearRing")
        etree.SubElement(lr_out, f"{{{ns}}}coordinates").text = _ring_coords_to_kml(poly.exterior)
        for hole in getattr(poly, "interiors", []):
            inner = etree.SubElement(parent_polygon_elem, f"{{{ns}}}innerBoundaryIs")
            lr_in = etree.SubElement(inner, f"{{{ns}}}LinearRing")
            etree.SubElement(lr_in, f"{{{ns}}}coordinates").text = _ring_coords_to_kml(hole)

    if geom.geom_type == "Polygon":
        write_one(geom)
    elif geom.geom_type == "MultiPolygon":
        for part in geom.geoms:
            poly_elem = etree.SubElement(parent_polygon_elem.getparent(), f"{{{ns}}}Polygon")
            write_one(part)

# -------------------- Grid Only KML generator --------------------
def generate_grid_only_kml(cells_ll, merged_ll):
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element(f"{{{ns}}}kml")
    doc = etree.SubElement(kml, f"{{{ns}}}Document")
    etree.SubElement(doc, f"{{{ns}}}name").text = "Grid Only"
    etree.SubElement(doc, f"{{{ns}}}description").text = "Developed by Rasipuram Range"

    style_grid = etree.SubElement(doc, f"{{{ns}}}Style", id="gridStyle")
    ls1 = etree.SubElement(style_grid, f"{{{ns}}}LineStyle")
    etree.SubElement(ls1, f"{{{ns}}}color").text = "ff0000ff"  # red
    etree.SubElement(ls1, f"{{{ns}}}width").text = "1"
    ps1 = etree.SubElement(style_grid, f"{{{ns}}}PolyStyle")
    etree.SubElement(ps1, f"{{{ns}}}fill").text = "0"

    for i, cell in enumerate(cells_ll, start=1):
        pm = etree.SubElement(doc, f"{{{ns}}}Placemark")
        etree.SubElement(pm, f"{{{ns}}}name").text = str(i)
        etree.SubElement(pm, f"{{{ns}}}styleUrl").text = "#gridStyle"
        poly_elem = etree.SubElement(pm, f"{{{ns}}}Polygon")
        _write_polygon_coords(ns, poly_elem, cell)

    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

# ----------------- Labeled + merged KML generator -----------------
def generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf=None):
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element(f"{{{ns}}}kml")
    doc = etree.SubElement(kml, f"{{{ns}}}Document")
    etree.SubElement(doc, f"{{{ns}}}name").text = "Labeled Grid"
    etree.SubElement(doc, f"{{{ns}}}description").text = "Developed by Rasipuram Range"

    # Styles
    style_grid = etree.SubElement(doc, f"{{{ns}}}Style", id="gridStyle")
    ls1 = etree.SubElement(style_grid, f"{{{ns}}}LineStyle")
    etree.SubElement(ls1, f"{{{ns}}}color").text = "ff0000ff"
    etree.SubElement(ls1, f"{{{ns}}}width").text = "1"
    ps1 = etree.SubElement(style_grid, f"{{{ns}}}PolyStyle")
    etree.SubElement(ps1, f"{{{ns}}}fill").text = "0"

    style_overlay = etree.SubElement(doc, f"{{{ns}}}Style", id="overlayStyle")
    ls2 = etree.SubElement(style_overlay, f"{{{ns}}}LineStyle")
    etree.SubElement(ls2, f"{{{ns}}}color").text = "ff00d7ff"
    etree.SubElement(ls2, f"{{{ns}}}width").text = "3"
    ps2 = etree.SubElement(style_overlay, f"{{{ns}}}PolyStyle")
    etree.SubElement(ps2, f"{{{ns}}}fill").text = "0"

    for i, cell in enumerate(cells_ll, start=1):
        area_ha = cell.area * (111000 ** 2) / 10000
        pm = etree.SubElement(doc, f"{{{ns}}}Placemark")
        etree.SubElement(pm, f"{{{ns}}}name").text = str(i)
        etree.SubElement(pm, f"{{{ns}}}styleUrl").text = "#gridStyle"

        html_table = f"""
        <table border="1" cellspacing="0" cellpadding="3">
          <tr><th>Field</th><th>Value</th></tr>
          <tr><td>ID</td><td>{i}</td></tr>
          <tr><td>Range</td><td>{user_inputs.get('range_name','')}</td></tr>
          <tr><td>RF</td><td>{user_inputs.get('rf_name','')}</td></tr>
          <tr><td>Beat</td><td>{user_inputs.get('beat_name','')}</td></tr>
          <tr><td>Year</td><td>{user_inputs.get('year_of_work','')}</td></tr>
          <tr><td>Area</td><td>{area_ha:.2f} ha</td></tr>
        </table>
        """.strip()

        desc = etree.SubElement(pm, f"{{{ns}}}description")
        desc.text = etree.CDATA(html_table)
        poly_elem = etree.SubElement(pm, f"{{{ns}}}Polygon")
        _write_polygon_coords(ns, poly_elem, cell)

    if overlay_gdf is not None and not overlay_gdf.empty:
        overlay_gdf = overlay_gdf.to_crs(4326)
        for geom in overlay_gdf.geometry:
            if geom.is_empty:
                continue
            pm = etree.SubElement(doc, f"{{{ns}}}Placemark")
            etree.SubElement(pm, f"{{{ns}}}name").text = "Overlay"
            etree.SubElement(pm, f"{{{ns}}}styleUrl").text = "#overlayStyle"
            poly_elem = etree.SubElement(pm, f"{{{ns}}}Polygon")
            _write_polygon_coords(ns, poly_elem, geom)

    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

# -------------------- PDF builder --------------------
def _accurate_area_ha_utm(geom_ll, utm_crs):
    area_m2 = gpd.GeoSeries([geom_ll], crs=4326).to_crs(utm_crs).area.iloc[0]
    return float(area_m2) / 10000.0
def build_pdf_report_standard(cells_ll, merged_ll, user_inputs, cell_size, overlay_present):
    import requests
    import tempfile

    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    # ✅ Try to load DejaVuSans dynamically
    try:
        temp_font_path = os.path.join(tempfile.gettempdir(), "DejaVuSans.ttf")
        if not os.path.exists(temp_font_path):
            # Download once (Google Fonts mirror)
            url = "https://github.com/dejavu-fonts/dejavu-fonts/raw/version_2_37/ttf/DejaVuSans.ttf"
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                with open(temp_font_path, "wb") as f:
                    f.write(r.content)

        pdf.add_font("DejaVu", "", temp_font_path, uni=True)
        pdf.add_font("DejaVu", "B", temp_font_path, uni=True)
        pdf.add_font("DejaVu", "I", temp_font_path, uni=True)
        pdf.set_font("DejaVu", "B", 14)
    except Exception as e:
        st.warning(f"⚠️ Could not load DejaVuSans font ({e}). Using default Helvetica instead.")
        pdf.set_font("Helvetica", "B", 14)

    # Determine UTM based on AOI centroid
    centroid = merged_ll.centroid
    utm = utm_crs_for_lonlat(centroid.x, centroid.y)

    # Prepare table rows
    rows = []
    total_area = 0.0
    for i, geom in enumerate(cells_ll, start=1):
        area_ha = _accurate_area_ha_utm(geom, utm)
        rows.append((i, area_ha, geom.centroid.y, geom.centroid.x))
        total_area += area_ha

    # Header section
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 14)
    pdf.cell(0, 8, "KML GRID GENERATOR v3.0 - FIELD REPORT", ln=1, align="C")
    pdf.ln(3)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "", 11)
    pdf.cell(0, 6, f"Range: {user_inputs.get('range_name','')} | RF: {user_inputs.get('rf_name','')}", ln=1)
    pdf.cell(0, 6, f"Beat: {user_inputs.get('beat_name','')} | Year: {user_inputs.get('year_of_work','')}", ln=1)
    pdf.ln(3)

    # Summary section
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 12)
    pdf.cell(0, 7, "Summary", ln=1)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "", 11)
    pdf.cell(0, 6, f"Total Cells: {len(rows)} | Total Area: {total_area:.2f} ha", ln=1)
    pdf.cell(0, 6, f"Cell Size: {cell_size} m | Overlay: {'Yes' if overlay_present else 'No'}", ln=1)
    pdf.cell(0, 6, f"Projection: WGS84 / {utm.to_string()}", ln=1)
    pdf.cell(0, 6, f"Generated on: {datetime.now().strftime('%d-%b-%Y %H:%M')}", ln=1)
    pdf.ln(3)

    # Table
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "B", 11)
    pdf.cell(25, 8, "Grid #", border=1, align="C")
    pdf.cell(35, 8, "Area (ha)", border=1, align="C")
    pdf.cell(65, 8, "Centroid Lat", border=1, align="C")
    pdf.cell(65, 8, "Centroid Lon", border=1, align="C")
    pdf.ln(8)

    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "", 10)
    for (idx, area, lat, lon) in rows:
        pdf.cell(25, 7, str(idx), border=1)
        pdf.cell(35, 7, f"{area:.2f}", border=1, align="R")
        pdf.cell(65, 7, f"{lat:.6f}", border=1, align="R")
        pdf.cell(65, 7, f"{lon:.6f}", border=1, align="R")
        pdf.ln(7)

    pdf.ln(4)
    pdf.set_font("DejaVu" if "DejaVu" in pdf.fonts else "Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "Note: Calculated using UTM projection for best accuracy. Generated by Rasipuram Range.")

    # ✅ Convert to bytes for Streamlit download
    result = pdf.output(dest="S")
    if isinstance(result, (bytes, bytearray)):
        return result
    elif hasattr(result, "getvalue"):
        return result.getvalue()
    elif isinstance(result, str):
        return result.encode("latin-1", errors="ignore")
    else:
        raise TypeError(f"Unexpected PDF output type: {type(result)}")

# -------------------------------------------------------------
# 🧭 SIDEBAR + MAIN APP
# -------------------------------------------------------------
st.sidebar.header("⚙️ Options")

uploaded_aoi = st.sidebar.file_uploader("Upload AOI KML/KMZ", type=["kml", "kmz"])
overlay_file = st.sidebar.file_uploader("Optional Overlay KML/KMZ", type=["kml", "kmz"])
cell_size = st.sidebar.number_input("Grid cell size (meters)", 10, 2000, 100, 10)

range_name = st.sidebar.text_input("Range Name", "Thammampatti")
rf_name = st.sidebar.text_input("RF Name", "Karumalai")
beat_name = st.sidebar.text_input("Beat Name", "A1")
year_of_work = st.sidebar.text_input("Year of Work", "2024")

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

# ---------------------------- MAIN DISPLAY ---------------------------
if st.session_state["generated"]:
    m = folium.Map(location=[11.0, 78.5], zoom_start=8)
    bounds = None
    overlay_gdf = None
    cells_ll = []
    merged_ll = None

    if uploaded_aoi:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".kml") as tmp:
            tmp.write(uploaded_aoi.read())
            tmp_path = tmp.name
        gdf = gpd.read_file(tmp_path, driver="KML")
        polygons = gdf.geometry
        cells_ll, merged_ll = make_grid_exact_clipped(polygons, cell_size)
        aoi_union = unary_union(polygons)
        folium.GeoJson(mapping(aoi_union), name="AOI", style_function=lambda x: {"color": "red"}).add_to(m)
        for cell in cells_ll:
            folium.GeoJson(mapping(cell), name="Grid", style_function=lambda x: {"color": "red"}).add_to(m)
        minx, miny, maxx, maxy = aoi_union.bounds
        bounds = [[miny, minx], [maxy, maxx]]

    if overlay_file:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".kml") as tmp2:
            tmp2.write(overlay_file.read())
            tmp2_path = tmp2.name
        overlay_gdf = gpd.read_file(tmp2_path, driver="KML")
        for geom in overlay_gdf.geometry:
            if not geom.is_empty:
                folium.GeoJson(mapping(geom), name="Overlay", style_function=lambda x: {"color": "#FFD700", "weight": 3}).add_to(m)
        if bounds is None and not overlay_gdf.empty:
            minx, miny, maxx, maxy = overlay_gdf.total_bounds
            bounds = [[miny, minx], [maxy, maxx]]

    if bounds:
        m.fit_bounds(bounds)

    st_folium(m, width=1200, height=700)

    if uploaded_aoi:
        user_inputs = st.session_state["user_inputs"]
        grid_count = len(cells_ll)
        total_area_ha = sum([c.area * (111000 ** 2) / 10000 for c in cells_ll])
        st.success(f"✅ Generated {grid_count} grid cells covering approximately {total_area_ha:.2f} ha inside AOI")

        grid_kml = generate_grid_only_kml(cells_ll, merged_ll)
        labeled_kml = generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf)

        st.markdown("### 💾 Downloads")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.download_button("📦 Download Grid Only KML", grid_kml, file_name="grid_only.kml", mime="application/vnd.google-earth.kml+xml")
        with col2:
            st.download_button("🧾 Download Labeled + Merged KML", labeled_kml, file_name="merged_labeled.kml", mime="application/vnd.google-earth.kml+xml")
        with col3:
            if generate_pdf:
                pdf_bytes = build_pdf_report_standard(cells_ll, merged_ll, user_inputs, cell_size, overlay_file is not None)
                st.download_button("📄 Download Report (PDF)", pdf_bytes, file_name="grid_report.pdf", mime="application/pdf")
    else:
        st.info("✅ Overlay loaded successfully (no grid generated — AOI not provided).")
else:
    st.info("👆 Upload AOI or Overlay files, click **➕ Add Input Labels**, then press **▶ Generate Grid**.")


