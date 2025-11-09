import streamlit as st
import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.geometry import mapping
from pyproj import CRS
import math, os, tempfile, requests
from streamlit_folium import st_folium
import folium
from fpdf import FPDF
from datetime import datetime

# ================================================================
# BASIC SETUP
# ================================================================
st.set_page_config(page_title="KML to Grid Generator v4.2 (Final)", layout="wide")
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
# HELPERS
# ================================================================
def utm_crs_for_lonlat(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)

def _accurate_area_ha_utm(geom_ll, utm_crs):
    area_m2 = gpd.GeoSeries([geom_ll], crs=4326).to_crs(utm_crs).area.iloc[0]
    return float(area_m2) / 10000.0

def make_grid_exact_clipped(polygons_ll, cell_size_m=100):
    merged_ll = unary_union(polygons_ll)
    centroid = merged_ll.centroid
    utm = utm_crs_for_lonlat(centroid.x, centroid.y)
    merged_utm = gpd.GeoSeries([merged_ll], crs=4326).to_crs(utm)
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

    # Back to WGS84 for export/preview
    cells_ll = [gpd.GeoSeries([geom], crs=utm).to_crs(4326).iloc[0] for geom in cells]
    return cells_ll, merged_ll

# ------------------ KML writers ------------------
from lxml import etree
def _ring_coords_to_kml(ring):
    return " ".join(f"{pt[0]},{pt[1]},0" for pt in ring.coords if len(pt) >= 2)

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

def generate_grid_only_kml(cells_ll, merged_ll):
    ns = "http://www.opengis.net/kml/2.2"
    kml = etree.Element("{%s}kml" % ns)
    doc = etree.SubElement(kml, "{%s}Document" % ns)
    etree.SubElement(doc, "{%s}name" % ns).text = "Grid Only"
    etree.SubElement(doc, "{%s}description" % ns).text = "Generated Grid"

    style = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls = etree.SubElement(style, "{%s}LineStyle" % ns)
    etree.SubElement(ls, "{%s}color" % ns).text = "ff0000ff"  # red (ABGR)
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
    etree.SubElement(doc, "{%s}name" % ns).text = "Labeled Grid + Overlay"

    # Grid style
    style_grid = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls1 = etree.SubElement(style_grid, "{%s}LineStyle" % ns)
    etree.SubElement(ls1, "{%s}color" % ns).text = "ff0000ff"    # red
    etree.SubElement(ls1, "{%s}width" % ns).text = "1"
    ps1 = etree.SubElement(style_grid, "{%s}PolyStyle" % ns)
    etree.SubElement(ps1, "{%s}fill" % ns).text = "0"

    # AOI style (not drawn as separate placemark, but you can add if needed)
    # Overlay style (golden yellow)
    style_overlay = etree.SubElement(doc, "{%s}Style" % ns, id="overlayStyle")
    ls2 = etree.SubElement(style_overlay, "{%s}LineStyle" % ns)
    etree.SubElement(ls2, "{%s}color" % ns).text = "ff00d7ff"    # ABGR -> golden yellow
    etree.SubElement(ls2, "{%s}width" % ns).text = "3"
    ps2 = etree.SubElement(style_overlay, "{%s}PolyStyle" % ns)
    etree.SubElement(ps2, "{%s}fill" % ns).text = "0"

    # Grid
    for i, cell in enumerate(cells_ll, start=1):
        pm = etree.SubElement(doc, "{%s}Placemark" % ns)
        etree.SubElement(pm, "{%s}name" % ns).text = f"{i}"
        etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"
        poly_elem = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly_elem, cell)

    # Overlay (if provided)
    if overlay_gdf is not None and not overlay_gdf.empty:
        og = overlay_gdf
        if og.crs is None:
            og = og.set_crs(4326)
        else:
            og = og.to_crs(4326)
        for geom in og.geometry:
            if geom.is_empty:
                continue
            pm = etree.SubElement(doc, "{%s}Placemark" % ns)
            etree.SubElement(pm, "{%s}name" % ns).text = "Overlay"
            etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#overlayStyle"
            poly_elem = etree.SubElement(pm, "{%s}Polygon" % ns)
            _write_polygon_coords(ns, poly_elem, geom)

    return etree.tostring(kml, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

# ================================================================
# PDF REPORT (single page)
# ================================================================
def build_pdf_report_standard(
    cells_ll, merged_ll, user_inputs, cell_size,
    overlay_gdf, title_text, density, area_invasive
):
    import matplotlib.pyplot as plt
    import contextily as ctx
    import tempfile
    import geopandas as gpd

    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    EMBLEM_PATH = os.path.join(os.path.dirname(__file__), "tn_emblem.png")

    # ================= HEADER =================
    def header_section():
        if os.path.exists(EMBLEM_PATH):
            pdf.image(EMBLEM_PATH, x=95, y=6, w=20)
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 30, "FOREST DEPARTMENT", align="C", new_x="LMARGIN", new_y="NEXT")

    # ================= FOOTER =================
    def footer_section():
        pdf.set_y(-15)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 10,
                 f"Developed by Rasipuram Range    |    Page {pdf.page_no()}",
                 0, 0, "C")

    # ================= PAGE 1 (MAP PAGE) =================
    pdf.add_page()
    header_section()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, title_text, align="C", new_x="LMARGIN", new_y="NEXT")

    tmp_dir = tempfile.gettempdir()
    map_img_path = os.path.join(tmp_dir, "composite_map.png")

    fig, ax = plt.subplots(figsize=(6.5, 5))
    ax.set_facecolor("white")

    merged_gdf = gpd.GeoSeries([merged_ll], crs="EPSG:4326").to_crs(3857)
    grid_gdf = gpd.GeoSeries(cells_ll, crs="EPSG:4326").to_crs(3857)
    merged_gdf.boundary.plot(ax=ax, color="red", linewidth=3)
    grid_gdf.boundary.plot(ax=ax, color="red", linewidth=1)

    if overlay_gdf is not None and not overlay_gdf.empty:
        overlay_gdf = overlay_gdf.to_crs(3857)
        overlay_gdf.boundary.plot(ax=ax, color="#FFD700", linewidth=3)

    ctx.add_basemap(ax, crs=3857, source=ctx.providers.Esri.WorldImagery)
    ax.axis("off")
    plt.tight_layout()
    fig.savefig(map_img_path, dpi=200, bbox_inches="tight")
    plt.close(fig)

    if os.path.exists(map_img_path):
        pdf.image(map_img_path, x=15, y=50, w=180)
    pdf.set_y(140)

    # ================= LEGEND =================
    pdf.set_font("Helvetica", "", 11)
    col1 = [
        f"Range: {user_inputs.get('range_name','')}",
        f"RF: {user_inputs.get('rf_name','')}",
        f"Beat: {user_inputs.get('beat_name','')}",
        f"Year of Work: {user_inputs.get('year_of_work','')}",
    ]
    col2 = [
        f"Density: {density}",
        f"Area of Invasive: {area_invasive} Ha",
        f"Cell Size: {cell_size} m",
        f"Overlay Included: {'Yes' if (overlay_gdf is not None and not overlay_gdf.empty) else 'No'}",
    ]
    y = pdf.get_y() + 8
    for i in range(4):
        pdf.text(25, y + i * 6, col1[i])
        pdf.text(110, y + i * 6, col2[i])
    pdf.ln(35)
    footer_section()

    # ================= PAGE 2+ (GRID DETAILS) =================
    pdf.add_page()
    header_section()

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 10, "Invasive Grid Area Details (within Overlay)",
              align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(15, 8, "S.No", 1, align="C")
    pdf.cell(35, 8, "Grid ID", 1, align="C")
    pdf.cell(70, 8, "Centroid Lat", 1, align="C")
    pdf.cell(70, 8, "Centroid Lon", 1, align="C")
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 10)

    overlay_union = None
    if overlay_gdf is not None and not overlay_gdf.empty:
        og = overlay_gdf
        og = og.set_crs(4326, allow_override=True)
        overlay_union = og.unary_union

    row_no = 1
    if overlay_union is not None:
        for idx, geom in enumerate(cells_ll, start=1):
            if overlay_union.intersects(geom):
                lat, lon = geom.centroid.y, geom.centroid.x
                pdf.cell(15, 7, str(row_no), 1)
                pdf.cell(35, 7, f"G{idx}", 1)
                pdf.cell(70, 7, f"{lat:.6f}", 1, align="R")
                pdf.cell(70, 7, f"{lon:.6f}", 1, align="R")
                pdf.ln(7)
                if pdf.get_y() > 270:
                    footer_section()
                    pdf.add_page()
                    header_section()
                    pdf.set_font("Helvetica", "B", 11)
                    pdf.cell(15, 8, "S.No", 1, align="C")
                    pdf.cell(35, 8, "Grid ID", 1, align="C")
                    pdf.cell(70, 8, "Centroid Lat", 1, align="C")
                    pdf.cell(70, 8, "Centroid Lon", 1, align="C")
                    pdf.ln(8)
                    pdf.set_font("Helvetica", "", 10)
                row_no += 1

    footer_section()

    # ================= RETURN SAFE BYTES =================
    result = pdf.output(dest="S")
    if isinstance(result, bytearray):
        result = bytes(result)
    elif isinstance(result, str):
        result = result.encode("latin1", errors="ignore")
    return result
# ================================================================
# SIDEBAR
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
# MAIN UI
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
        if overlay_gdf.crs is None:
            overlay_gdf = overlay_gdf.set_crs(4326)
        else:
            overlay_gdf = overlay_gdf.to_crs(4326)
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
        grid_kml = generate_grid_only_kml(cells_ll, merged_ll)
        merged_kml = generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf)

        st.markdown("### 💾 Downloads")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.download_button("📦 Download Grid Only KML",
                               grid_kml, file_name="grid_only.kml",
                               mime="application/vnd.google-earth.kml+xml")
        with c2:
            st.download_button("🧾 Download Labeled + Overlay KML",
                               merged_kml, file_name="merged_labeled.kml",
                               mime="application/vnd.google-earth.kml+xml")
        with c3:
            if generate_pdf:
                pdf_bytes = build_pdf_report_standard(
                    cells_ll, merged_ll, user_inputs, cell_size,
                    overlay_gdf, title_text, density, area_invasive
                )
                st.download_button("📄 Download Invasive Report (PDF)",
                                   data=pdf_bytes, file_name="Invasive_Report.pdf",
                                   mime="application/pdf")
else:
    st.info("👆 Upload AOI, add labels, then click ▶ Generate Grid.")



