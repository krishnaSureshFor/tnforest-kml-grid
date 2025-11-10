import streamlit as st
import geopandas as gpd
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.geometry import mapping, Polygon, MultiPolygon
from pyproj import CRS
import math, os, tempfile, zipfile
from streamlit_folium import st_folium
import folium
from fpdf import FPDF
import matplotlib.pyplot as plt
import contextily as ctx
from lxml import etree

# ================================================================
# APP CONFIG
# ================================================================
st.set_page_config(page_title="KML to Grid Generator v4.3", layout="wide")
st.title("🗺️ KML to Grid Generator v4.3 (KMZ Supported)")

# ================================================================
# STATE INIT
# ================================================================
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
            x0, y0 = minx + i * cell_size_m, miny + j * cell_size_m
            cell = box(x0, y0, x0 + cell_size_m, y0 + cell_size_m)
            if aoi_union.intersects(cell):
                inter = cell.intersection(aoi_union)
                if not inter.is_empty:
                    cells.append(inter)
    cells_ll = [gpd.GeoSeries([geom], crs=utm).to_crs(4326).iloc[0] for geom in cells]
    return cells_ll, merged_ll

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
    style = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls = etree.SubElement(style, "{%s}LineStyle" % ns)
    etree.SubElement(ls, "{%s}color" % ns).text = "ff0000ff"
    etree.SubElement(ls, "{%s}width" % ns).text = "1"
    ps = etree.SubElement(style, "{%s}PolyStyle" % ns)
    etree.SubElement(ps, "{%s}fill" % ns).text = "0"
    for i, cell in enumerate(cells_ll, start=1):
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
    style_grid = etree.SubElement(doc, "{%s}Style" % ns, id="gridStyle")
    ls1 = etree.SubElement(style_grid, "{%s}LineStyle" % ns)
    etree.SubElement(ls1, "{%s}color" % ns).text = "ff0000ff"
    etree.SubElement(ls1, "{%s}width" % ns).text = "1"
    ps1 = etree.SubElement(style_grid, "{%s}PolyStyle" % ns)
    etree.SubElement(ps1, "{%s}fill" % ns).text = "0"
    style_overlay = etree.SubElement(doc, "{%s}Style" % ns, id="overlayStyle")
    ls2 = etree.SubElement(style_overlay, "{%s}LineStyle" % ns)
    etree.SubElement(ls2, "{%s}color" % ns).text = "ff00d7ff"
    etree.SubElement(ls2, "{%s}width" % ns).text = "3"
    ps2 = etree.SubElement(style_overlay, "{%s}PolyStyle" % ns)
    etree.SubElement(ps2, "{%s}fill" % ns).text = "0"
    for i, cell in enumerate(cells_ll, start=1):
        pm = etree.SubElement(doc, "{%s}Placemark" % ns)
        etree.SubElement(pm, "{%s}name" % ns).text = str(i)
        etree.SubElement(pm, "{%s}styleUrl" % ns).text = "#gridStyle"
        poly_elem = etree.SubElement(pm, "{%s}Polygon" % ns)
        _write_polygon_coords(ns, poly_elem, cell)
    if overlay_gdf is not None and not overlay_gdf.empty:
        og = overlay_gdf.to_crs(4326)
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
# PDF GENERATOR (from your final version)
# ================================================================
def build_pdf_report_standard(cells_ll, merged_ll, user_inputs, cell_size, overlay_gdf, title_text, density, area_invasive):
    from shapely.geometry import Polygon, MultiPolygon
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    EMBLEM_PATH = os.path.join(os.path.dirname(__file__), "tn_emblem.png")

    # ---------------- Header ----------------
    def header_section():
        if os.path.exists(EMBLEM_PATH):
            pdf.image(EMBLEM_PATH, x=93, y=8, w=25)
        pdf.set_y(35)
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "FOREST DEPARTMENT", align="C", ln=1)

    # ---------------- Page 1 (Map + Legend) ----------------
    pdf.add_page()
    header_section()
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, title_text, align="C", ln=1)

    # ---- Generate Map ----
    tmp_dir = tempfile.gettempdir()
    map_img_path = os.path.join(tmp_dir, "map_overlay.png")

    # Generate and save map image
    fig, ax = plt.subplots(figsize=(7, 5.8))  # fixed physical size for consistent output
    ax.set_facecolor("white")

    merged_gdf = gpd.GeoSeries([merged_ll], crs="EPSG:4326").to_crs(3857)
    grid_gdf = gpd.GeoSeries(cells_ll, crs="EPSG:4326").to_crs(3857)
    merged_gdf.boundary.plot(ax=ax, color="red", linewidth=3, label="AOI")
    grid_gdf.boundary.plot(ax=ax, color="red", linewidth=1, label="Grid")

    if overlay_gdf is not None and not overlay_gdf.empty:
        overlay_gdf = overlay_gdf.to_crs(3857)
        overlay_gdf.boundary.plot(ax=ax, color="#FFD700", linewidth=3, label="Overlay")

    ctx.add_basemap(
    ax,
    crs=3857,
    source=ctx.providers.Esri.WorldImagery,
    zoom=14,
    attribution=False  # ✅ disables the ESRI/Leaflet attribution text
    ax.patch.set_facecolor("white")
    ax.patch.set_alpha(0.9)
    )
    ax.axis("off")
    plt.tight_layout(pad=0.1)
    fig.savefig(map_img_path, dpi=250, bbox_inches="tight")
    plt.close(fig)

    # ---- Insert Map ----
    # Fixed position + fixed height (3/4 page)
    pdf.image(map_img_path, x=15, y=55, w=180, h=145)

    # ---- Legend below map ----
    legend_y = 55 + 145 + 8  # 8mm gap below map
    pdf.set_y(legend_y)
    pdf.set_fill_color(245, 245, 240)
    pdf.set_draw_color(180, 180, 180)
    pdf.rect(15, legend_y, 180, 40, style="FD")

    pdf.set_font("Helvetica", "", 11)
    y_start = legend_y + 10
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
        f"Overlay: {'Yes' if overlay_gdf is not None and not overlay_gdf.empty else 'No'}",
    ]

    for i in range(4):
        pdf.text(25, y_start + i * 6, col1[i])
        pdf.text(115, y_start + i * 6, col2[i])

    # ---- Note below legend ----
    note_y = legend_y + 45
    pdf.set_y(note_y)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 5, "Note: Satellite background and boundaries are automatically generated. Developed by Rasipuram Range.")
    pdf.set_text_color(0, 0, 0)

    # ---------------- Page 2 (Corner GPS Table) ----------------
    pdf.add_page()
    header_section()
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 10, "Corner GPS of Overlay Area", ln=1, align="C")

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(25, 8, "S.No", 1, align="C")
    pdf.cell(75, 8, "Latitude", 1, align="C")
    pdf.cell(75, 8, "Longitude", 1, align="C")
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 10)

    row_no = 1
    if overlay_gdf is not None and not overlay_gdf.empty:
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
                pdf.cell(25, 7, str(row_no), 1)
                pdf.cell(75, 7, f"{lat:.6f}", 1, align="R")
                pdf.cell(75, 7, f"{lon:.6f}", 1, align="R")
                pdf.ln(7)
                row_no += 1
                if pdf.get_y() > 265:
                    pdf.add_page()
                    header_section()
                    pdf.ln(2)
                    pdf.set_font("Helvetica", "B", 11)
                    pdf.cell(25, 8, "S.No", 1, align="C")
                    pdf.cell(75, 8, "Latitude", 1, align="C")
                    pdf.cell(75, 8, "Longitude", 1, align="C")
                    pdf.ln(8)
                    pdf.set_font("Helvetica", "", 10)
    else:
        pdf.cell(0, 8, "No overlay polygons detected.", 1, align="C")

    result = pdf.output(dest="S")
    if isinstance(result, bytearray):
        return bytes(result)
    return result.encode("latin1", errors="ignore")
# ================================================================
# SIDEBAR UI
# ================================================================
st.sidebar.header("⚙️ Options")
uploaded_aoi = st.sidebar.file_uploader("Upload AOI KML/KMZ", type=["kml", "kmz"])
overlay_file = st.sidebar.file_uploader("Optional Overlay KML/KMZ", type=["kml", "kmz"])
cell_size = st.sidebar.number_input("Grid cell size (m)", 10, 2000, 100, 10)
range_name = st.sidebar.text_input("Range Name", "Thammampatti")
rf_name = st.sidebar.text_input("RF Name", "Karumalai")
beat_name = st.sidebar.text_input("Beat Name", "A1")
year_of_work = st.sidebar.text_input("Year of Work", "2024")
title_text = st.sidebar.text_input("🧭 Report Title", "Removal of Invasive Species - Thammampatti Range")
density = st.sidebar.text_input("Density", "Medium")
area_invasive = st.sidebar.text_input("Area of Invasive (Ha)", "5")
if st.sidebar.button("➕ Add Input Labels"):
    st.session_state["user_inputs"] = {
        "range_name": range_name, "rf_name": rf_name, "beat_name": beat_name, "year_of_work": year_of_work
    }
    st.sidebar.success("✅ Labels saved.")
generate_pdf = st.sidebar.checkbox("📄 Generate PDF Report", value=True)
col1, col2 = st.sidebar.columns(2)
with col1: generate_click = st.button("▶ Generate Grid")
with col2: reset_click = st.button("🔄 Reset Map")
if reset_click:
    st.session_state.clear()
    init_state()
    st.rerun()
if generate_click:
    st.session_state["generated"] = True

# ================================================================
# MAIN SECTION
# ================================================================
if st.session_state["generated"]:
    m = folium.Map(location=[11.0, 78.5], zoom_start=8)
    bounds, overlay_gdf = None, None
    cells_ll, merged_ll = [], None

    # AOI load (KML/KMZ)
    if uploaded_aoi is not None:
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
        gdf = gpd.read_file(tmp_path, driver="KML")
        polygons = gdf.geometry
        cells_ll, merged_ll = make_grid_exact_clipped(polygons, cell_size)
        aoi_union = unary_union(polygons)
        folium.GeoJson(mapping(aoi_union),
                       name="AOI", style_function=lambda x: {"color": "red", "weight": 3, "fillOpacity": 0}).add_to(m)
        for cell in cells_ll:
            folium.GeoJson(mapping(cell),
                           style_function=lambda x: {"color": "red", "weight": 1, "fillOpacity": 0}).add_to(m)
        bounds = [[aoi_union.bounds[1], aoi_union.bounds[0]],
                  [aoi_union.bounds[3], aoi_union.bounds[2]]]

    # Overlay load (KML/KMZ)
    if overlay_file is not None:
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
        overlay_gdf = gpd.read_file(tmp2_path, driver="KML").to_crs(4326)
        for geom in overlay_gdf.geometry:
            if not geom.is_empty:
                folium.GeoJson(mapping(geom),
                               style_function=lambda x: {"color": "#FFD700", "weight": 3, "fillOpacity": 0}).add_to(m)
        if bounds is None:
            bounds = [[overlay_gdf.total_bounds[1], overlay_gdf.total_bounds[0]],
                      [overlay_gdf.total_bounds[3], overlay_gdf.total_bounds[2]]]

    if bounds:
        m.fit_bounds(bounds)
    st_folium(m, width=1200, height=700)

    # Downloads
    user_inputs = st.session_state["user_inputs"]
    grid_kml = generate_grid_only_kml(cells_ll, merged_ll)
    labeled_kml = generate_labeled_kml(cells_ll, merged_ll, user_inputs, overlay_gdf)
    st.markdown("### 💾 Downloads")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("📦 Download Grid Only KML", grid_kml, file_name="grid_only.kml", mime="application/vnd.google-earth.kml+xml")
    with c2:
        st.download_button("🧾 Download Labeled + Overlay KML", labeled_kml, file_name="merged_labeled.kml", mime="application/vnd.google-earth.kml+xml")
    with c3:
        if generate_pdf:
            pdf_bytes = build_pdf_report_standard(cells_ll, merged_ll, user_inputs, cell_size, overlay_gdf, title_text, density, area_invasive)
            st.download_button("📄 Download Invasive Report (PDF)", pdf_bytes, file_name="Invasive_Report.pdf", mime="application/pdf")
else:
    st.info("👆 Upload AOI (KML/KMZ), optionally Overlay, add labels, then click ▶ Generate Grid.")




