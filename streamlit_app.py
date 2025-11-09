import streamlit as st
import geopandas as gpd
from shapely.geometry import box, mapping
from shapely.ops import unary_union
from pyproj import CRS
import math, tempfile, os, requests
from fpdf import FPDF
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import contextily as ctx
from datetime import datetime
from streamlit_folium import st_folium
import folium

# ----------------------------------------------------------------------
# INITIAL SETUP
# ----------------------------------------------------------------------
st.set_page_config(page_title="KML to Grid Generator v4.1", layout="wide")
st.title("🗺️ KML to Grid Generator v4.1 — Rasipuram Range")

# ----------------------------------------------------------------------
# AUTO-DOWNLOAD FONT AND EMBLEM
# ----------------------------------------------------------------------
ROOT_DIR = os.path.dirname(__file__)
FONT_PATH = os.path.join(ROOT_DIR, "DejaVuSans.ttf")
EMBLEM_PATH = os.path.join(ROOT_DIR, "tn_emblem.png")

# ✅ Auto-download DejaVuSans if missing
if not os.path.exists(FONT_PATH):
    try:
        url = "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf"
        r = requests.get(url, timeout=20)
        with open(FONT_PATH, "wb") as f:
            f.write(r.content)
        st.sidebar.info("Downloaded DejaVuSans.ttf automatically.")
    except Exception as e:
        st.sidebar.warning(f"⚠️ Font download failed: {e}")

# ✅ Auto-download TN Emblem if missing
if not os.path.exists(EMBLEM_PATH):
    try:
        emblem_url = "https://upload.wikimedia.org/wikipedia/commons/6/6f/Emblem_of_Tamil_Nadu.svg.png"
        r = requests.get(emblem_url, timeout=20)
        with open(EMBLEM_PATH, "wb") as f:
            f.write(r.content)
        st.sidebar.info("Downloaded Tamil Nadu Emblem automatically.")
    except Exception as e:
        st.sidebar.warning(f"⚠️ Emblem download failed: {e}")

# ----------------------------------------------------------------------
# STATE INITIALIZATION
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
# CRS HELPER
# ----------------------------------------------------------------------
def utm_crs_for_lonlat(lon, lat):
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)

# ----------------------------------------------------------------------
# GRID GENERATION
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
# PDF BUILDER FUNCTION (Compact Map + Legend + Tables)
# ----------------------------------------------------------------------
def build_pdf_report_standard(cells_ll, merged_ll, overlay_gdf, user_inputs,
                              cell_size, overlay_present, title_text, density, area_invasive):
    aoi_gdf = gpd.GeoSeries([merged_ll], crs="EPSG:4326").to_crs(3857)
    grid_gdf = gpd.GeoDataFrame(geometry=cells_ll, crs="EPSG:4326").to_crs(3857)
    overlay_gdf = overlay_gdf.to_crs(3857) if overlay_gdf is not None else None

    # Tight extent: AOI + overlay
    full_extent = aoi_gdf.total_bounds
    if overlay_gdf is not None and not overlay_gdf.empty:
        ob = overlay_gdf.total_bounds
        full_extent = [min(full_extent[0], ob[0]), min(full_extent[1], ob[1]),
                       max(full_extent[2], ob[2]), max(full_extent[3], ob[3])]
    buffer = 500

    fig, ax = plt.subplots(figsize=(8, 4.8))
    ax.set_xlim(full_extent[0]-buffer, full_extent[2]+buffer)
    ax.set_ylim(full_extent[1]-buffer, full_extent[3]+buffer)
    try:
        ctx.add_basemap(ax, source=ctx.providers.Esri.WorldImagery, zoom=14, attribution=False)
    except Exception:
        pass
    aoi_gdf.boundary.plot(ax=ax, color="#FF0000", linewidth=3)
    grid_gdf.boundary.plot(ax=ax, color="#FF0000", linewidth=1)
    if overlay_gdf is not None and not overlay_gdf.empty:
        overlay_gdf.boundary.plot(ax=ax, color="#FFD700", linewidth=3)
    ax.set_axis_off()

    # North arrow
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    cc = (xlim[1] - (xlim[1]-xlim[0])*0.07, ylim[1] - (ylim[1]-ylim[0])*0.1)
    r = (xlim[1]-xlim[0])*0.015
    ax.add_patch(patches.Circle(cc, r, edgecolor="black", facecolor="#2E8B57", lw=2))
    ax.annotate("", xy=(cc[0], cc[1]+r*0.6), xytext=(cc[0], cc[1]-r*0.4),
                arrowprops=dict(facecolor="white", edgecolor="white", width=3, headwidth=8))
    ax.text(cc[0], cc[1]-r*0.8, "N", ha="center", va="center",
            fontsize=12, fontweight="bold", color="white")

    plt.tight_layout(pad=0)
    img_path = os.path.join(tempfile.gettempdir(), "map_topview.png")
    plt.savefig(img_path, bbox_inches="tight", pad_inches=0.1, dpi=150)
    plt.close(fig)

    # === PDF CREATION ===
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    if os.path.exists(FONT_PATH):
        pdf.add_font("DejaVu","",FONT_PATH,uni=True)
        pdf.add_font("DejaVu","B",FONT_PATH,uni=True)
        pdf.add_font("DejaVu","I",FONT_PATH,uni=True)

    def add_header():
        pdf.set_fill_color(0, 100, 0)
        pdf.rect(0, 0, 210, 20, "F")
        pdf.set_y(4)
        pdf.set_text_color(255,255,255)
        pdf.set_font("DejaVu", "B", 18)
        pdf.cell(65,10,"FOREST",align="R")
        pdf.cell(60,10,"",align="C")
        pdf.cell(65,10,"DEPARTMENT",ln=1,align="L")
        if os.path.exists(EMBLEM_PATH):
            pdf.image(EMBLEM_PATH, x=90, y=3, w=30, h=30)
        pdf.set_y(26)
        pdf.set_draw_color(0,100,0)
        pdf.line(15,pdf.get_y(),195,pdf.get_y())
        pdf.ln(8)
        pdf.set_text_color(0,0,0)

    # PAGE 1
    pdf.add_page()
    add_header()
    pdf.set_font("DejaVu","B",14)
    pdf.cell(0,8,title_text,ln=1,align="C")
    pdf.ln(4)
    pdf.image(img_path, x=15, y=pdf.get_y(), w=180, h=95)
    pdf.rect(15, pdf.get_y(), 180, 95)
    pdf.ln(100)
    pdf.set_fill_color(255,255,255)
    pdf.set_draw_color(0,100,0)
    start_y = pdf.get_y()
    pdf.rect(15,start_y,180,50)
    pdf.set_xy(20,start_y+4)
    pdf.set_text_color(0,100,0)
    pdf.set_font("DejaVu","B",12)
    pdf.cell(0,7,"Legend",ln=1)
    pdf.set_text_color(0,0,0)
    pdf.set_font("DejaVu","",11)
    left = [f"Range: {user_inputs.get('range_name','')}",
            f"Beat: {user_inputs.get('beat_name','')}",
            f"Grid Size: {cell_size} m",
            f"Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}"]
    right = [f"RF: {user_inputs.get('rf_name','')}",
             f"Density: {density}",
             f"Area of Invasive: {area_invasive} Ha",
             f"Overlay: {'Yes' if overlay_present else 'No'}"]
    for l,r in zip(left,right):
        pdf.cell(85,6,l)
        pdf.cell(85,6,r,ln=1)
    pdf.set_y(-18)
    pdf.set_font("DejaVu","I",9)
    pdf.multi_cell(0,5,"Developed by Rasipuram Range",align="C")

    # PAGE 2
    pdf.add_page()
    add_header()
    pdf.set_font("DejaVu","B",13)
    pdf.cell(0,8,"Invasive Grid Area Details",ln=1,align="C")
    pdf.set_font("DejaVu","B",11)
    pdf.cell(20,8,"SL No",1,0,"C")
    pdf.cell(40,8,"Grid ID",1,0,"C")
    pdf.cell(40,8,"Area (Ha)",1,0,"C")
    pdf.cell(45,8,"Latitude",1,0,"C")
    pdf.cell(45,8,"Longitude",1,1,"C")

    total_area=0
    pdf.set_font("DejaVu","",10)
    for i,geom in enumerate(cells_ll,start=1):
        centroid=geom.centroid
        utm=gpd.GeoSeries([geom],crs=4326).estimate_utm_crs()
        area_ha=float(gpd.GeoSeries([geom],crs=4326).to_crs(utm).area.iloc[0])/10000
        total_area+=area_ha
        pdf.cell(20,6,str(i),1,0,"C")
        pdf.cell(40,6,f"Grid-{i}",1,0,"C")
        pdf.cell(40,6,f"{area_ha:.2f}",1,0,"R")
        pdf.cell(45,6,f"{centroid.y:.6f}",1,0,"R")
        pdf.cell(45,6,f"{centroid.x:.6f}",1,1,"R")

    pdf.set_font("DejaVu","B",11)
    pdf.cell(60,8,"TOTAL",1,0,"C")
    pdf.cell(40,8,f"{total_area:.2f}",1,0,"R")
    pdf.cell(90,8,"",1,1,"C")

    result = pdf.output(dest="S")
    return bytes(result) if isinstance(result,(bytes,bytearray)) else result.encode("latin1","ignore")

# ----------------------------------------------------------------------
# SIDEBAR
# ----------------------------------------------------------------------
st.sidebar.header("⚙️ Options")
uploaded_aoi = st.sidebar.file_uploader("Upload AOI KML/KMZ", type=["kml","kmz"])
overlay_file = st.sidebar.file_uploader("Optional Overlay KML/KMZ", type=["kml","kmz"])
cell_size = st.sidebar.number_input("Grid cell size (m)", 10, 2000, 100, 10)
range_name = st.sidebar.text_input("Range Name", "Thammampatty")
rf_name = st.sidebar.text_input("RF Name", "Paithur RF")
beat_name = st.sidebar.text_input("Beat Name", "Paithur South")
year_of_work = st.sidebar.text_input("Year of Work", "2024")
title_text = st.sidebar.text_input("Report Title", "Removal of Invasive Species — Thammampatty Range")
density = st.sidebar.text_input("Density", "Medium")
area_invasive = st.sidebar.text_input("Area of Invasive (Ha)", "5")

if st.sidebar.button("➕ Add Input Labels"):
    st.session_state["user_inputs"] = {
        "range_name": range_name, "rf_name": rf_name,
        "beat_name": beat_name, "year_of_work": year_of_work
    }
    st.sidebar.success("✅ Labels added.")

generate_pdf = st.sidebar.checkbox("📄 Generate PDF Report", True)
if st.sidebar.button("▶ Generate Grid"):
    st.session_state["generated"] = True

# ----------------------------------------------------------------------
# MAIN AREA
# ----------------------------------------------------------------------
if st.session_state["generated"]:
    m = folium.Map(location=[11,78.5], zoom_start=8)
    overlay_gdf, cells_ll, merged_ll = None, [], None
    if uploaded_aoi:
        with tempfile.NamedTemporaryFile(delete=False,suffix=".kml") as tmp:
            tmp.write(uploaded_aoi.read())
            tmp_path=tmp.name
        gdf=gpd.read_file(tmp_path,driver="KML")
        polygons=gdf.geometry
        cells_ll,merged_ll=make_grid_exact_clipped(polygons,cell_size)
        aoi_union=unary_union(polygons)
        folium.GeoJson(mapping(aoi_union),name="AOI",
                       style_function=lambda x:{"color":"red","weight":3,"fillOpacity":0}).add_to(m)
        for cell in cells_ll:
            folium.GeoJson(mapping(cell),name="Grid",
                           style_function=lambda x:{"color":"red","weight":1,"fillOpacity":0}).add_to(m)
        m.fit_bounds([[aoi_union.bounds[1],aoi_union.bounds[0]],
                      [aoi_union.bounds[3],aoi_union.bounds[2]]])

    if overlay_file:
        with tempfile.NamedTemporaryFile(delete=False,suffix=".kml") as tmp2:
            tmp2.write(overlay_file.read())
            tmp2_path=tmp2.name
        overlay_gdf=gpd.read_file(tmp2_path,driver="KML")
        for geom in overlay_gdf.geometry:
            if not geom.is_empty:
                folium.GeoJson(mapping(geom),name="Overlay",
                               style_function=lambda x:{"color":"#FFD700","weight":3,"fillOpacity":0}).add_to(m)
    st_folium(m,width=1200,height=700)

    if uploaded_aoi:
        st.success("✅ Grid generated successfully.")
        if generate_pdf:
            pdf_bytes=build_pdf_report_standard(
                cells_ll,merged_ll,overlay_gdf,
                st.session_state["user_inputs"],cell_size,
                overlay_file is not None,title_text,density,area_invasive)
            st.download_button("📄 Download Report (PDF)",pdf_bytes,
                               file_name="forest_grid_report.pdf",mime="application/pdf")

else:
    st.info("👆 Upload AOI, set labels, and click ▶ Generate Grid.")
