import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import json
import ast
import os
from datetime import datetime, timezone, timedelta

# Base dir (รองรับทั้ง local และ cloud deploy)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# GOOGLE OAUTH 2.0
# ============================================================
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/cloud-platform",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def _oauth_config() -> dict | None:
    """อ่าน OAuth config จาก .streamlit/secrets.toml"""
    try:
        return {
            "client_id":     st.secrets["GOOGLE_CLIENT_ID"],
            "client_secret": st.secrets["GOOGLE_CLIENT_SECRET"],
            "redirect_uri":  st.secrets.get("OAUTH_REDIRECT_URI", "http://localhost:8501"),
        }
    except (KeyError, FileNotFoundError):
        return None


def _make_flow(cfg: dict):
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(
        {"web": {
            "client_id":     cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "auth_uri":  "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [cfg["redirect_uri"]],
        }},
        scopes=OAUTH_SCOPES,
        redirect_uri=cfg["redirect_uri"],
    )


def handle_oauth_callback() -> bool:
    """ดักจับ ?code=... จาก Google แล้วแลก token — เรียกก่อน render ทุกครั้ง"""
    if "code" not in st.query_params:
        return False
    cfg = _oauth_config()
    if not cfg:
        return False
    try:
        flow = _make_flow(cfg)
        flow.fetch_token(code=st.query_params["code"])
        creds = flow.credentials
        st.session_state["_oauth_creds"] = {
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes or OAUTH_SCOPES),
        }
        # ดึง email ผู้ใช้
        import requests as _req
        r = _req.get(
            "https://www.googleapis.com/oauth2/v1/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=5,
        )
        if r.ok:
            info = r.json()
            st.session_state["_oauth_email"] = info.get("email", "")
            st.session_state["_oauth_name"]  = info.get("name", "")
        st.query_params.clear()
        return True
    except Exception as e:
        st.error(f"❌ Google login ล้มเหลว: {e}")
        return False


def get_oauth_credentials():
    """คืน google.oauth2.credentials.Credentials ที่ valid หรือ None"""
    from google.oauth2.credentials import Credentials as OAuthCreds
    from google.auth.transport.requests import Request as GRequest
    d = st.session_state.get("_oauth_creds")
    if not d:
        return None
    creds = OAuthCreds(
        token=d["token"],
        refresh_token=d.get("refresh_token"),
        token_uri=d["token_uri"],
        client_id=d["client_id"],
        client_secret=d["client_secret"],
        scopes=d["scopes"],
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GRequest())
            d["token"] = creds.token
            st.session_state["_oauth_creds"] = d
        except Exception:
            st.session_state.pop("_oauth_creds", None)
            return None
    return creds


def sidebar_oauth_section():
    """แสดง Login button หรือ user info — return (creds, email) หรือ (None, None)"""
    cfg = _oauth_config()
    if not cfg:
        return None, None   # ไม่ได้ตั้งค่า OAuth → ใช้ SA key แทน

    creds = get_oauth_credentials()
    if creds:
        email = st.session_state.get("_oauth_email", "")
        name  = st.session_state.get("_oauth_name", "")
        st.markdown(f"""
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;
                    padding:10px 14px;margin:4px 0;">
            <div style="font-size:0.9em;font-weight:700;color:#15803d;">✅ เชื่อมต่อแล้ว</div>
            <div style="font-size:0.82em;color:#166534;margin-top:2px;">👤 {name or email}</div>
        </div>""", unsafe_allow_html=True)
        if st.button("🚪 Logout", key="oauth_logout", use_container_width=True):
            for k in ["_oauth_creds","_oauth_email","_oauth_name","df","client","project","scores"]:
                st.session_state.pop(k, None)
            st.rerun()
        return creds, email

    # ยังไม่ได้ login — แสดงปุ่ม
    flow = _make_flow(cfg)
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    st.markdown("""
    <style>
    div[data-testid="stLinkButton"] a {
        background-color: #4285F4 !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        font-size: 0.9em !important;
        box-shadow: 0 2px 6px rgba(66,133,244,0.4) !important;
    }
    div[data-testid="stLinkButton"] a:hover {
        background-color: #3367D6 !important;
        box-shadow: 0 4px 10px rgba(66,133,244,0.5) !important;
    }
    </style>
    """, unsafe_allow_html=True)
    st.link_button("🔵 Sign in with Google", auth_url, use_container_width=True)
    st.caption("ใช้ Google account เดียวกับที่เข้า BigQuery")
    return None, None

# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(
    page_title="BU Data Score",
    page_icon="🏆",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============================================================
# DIMENSION CONFIG
# ============================================================
DIMENSIONS = [
    {
        "id": "sufficient", "no": 1, "en": "Sufficient", "thai": "เพียงพอ",
        "weight": 0.10, "icon": "📊", "mode": "manual",
        "check": ["จำนวน Record เพียงพอสำหรับ Train/Test", "มีข้อมูลย้อนหลังครอบคลุม Seasonal"],
        "bq_source": "ต้องการ date column — ระบุชื่อ column เพื่อให้ query ได้",
        "guidance": "ตรวจสอบ: COUNT(*) ของแต่ละตาราง และ MIN/MAX ของ transaction date\nถ้าระบุชื่อ date column ได้ระบบจะ query ให้อัตโนมัติ",
        "levels": {
            1: "ข้อมูลย้อนหลัง < 3 เดือน มีแค่ข้อมูลสรุปยอดรวม",
            2: "ข้อมูล 3–6 เดือน เริ่มเห็นรายละเอียดสินค้า/บริการ",
            3: "ข้อมูลย้อนหลังครบ 1 ปี มีพื้นฐานครบ (ใคร/อะไร/ที่ไหน/เมื่อไหร่)",
            4: "ข้อมูล > 2 ปี และมีปัจจัยภายนอก (โปรโมชั่น/คู่แข่ง)",
            5: "ข้อมูลหลายปี มีข้อมูลเชิงลึกรายบุคคล (Segment/Person level)",
        },
    },
    {
        "id": "quality", "no": 2, "en": "Quality", "thai": "คุณภาพ",
        "weight": 0.20, "icon": "✅", "mode": "auto",
        "dq_dims": ["ACCURACY", "COMPLETENESS"],
        "check": ["ความถูกต้อง (Accuracy)", "ความครบถ้วน — Field สำคัญไม่เป็น Null/Default มั่วๆ"],
        "levels": {
            1: "ข้อมูลผิดหรือว่างเยอะ (>30%) จนนำไปคำนวณต่อไม่ได้",
            2: "รู้ว่ามีจุดผิดเยอะ แต่ไม่มีระบบตรวจ ต้องไล่แก้เองเป็นครั้งๆ",
            3: "มีรายงานสรุปคุณภาพข้อมูล (% ค่าว่าง/ค่าผิด รายเดือน)",
            4: "มีระบบตรวจสอบและดักจับข้อมูลผิดตั้งแต่ขั้นตอนการกรอก",
            5: "ข้อมูลสะอาดเกือบ 100% มีระบบแจ้งเตือน/แก้ไขอัตโนมัติ",
        },
    },
    {
        "id": "consistency", "no": 3, "en": "Consistency", "thai": "สม่ำเสมอ",
        "weight": 0.15, "icon": "🔄", "mode": "auto",
        "dq_dims": ["CONSISTENCY"],
        "check": ["หน่วยวัด (Unit) และรูปแบบ (Format) ตรงกันทั้งชุด", "ใช้มาตรฐาน Schema เดียวกัน"],
        "levels": {
            1: "ต่างคนต่างเก็บ ชื่อฟิลด์/รหัสในแต่ละไฟล์ไม่ตรงกัน",
            2: "เริ่มตกลงมาตรฐานชื่อ/หน่วยกันบ้าง แต่ยังต่างคนต่างทำ",
            3: "มี Data Dictionary กลางที่ทุกคนใน BU ใช้ร่วมกัน",
            4: "ทุกระบบใช้รหัสสินค้า/ลูกค้าและรูปแบบวันที่เดียวกัน",
            5: "ข้อมูลทั้งองค์กรใช้มาตรฐานเดียวกัน เป็น SSOT",
        },
    },
    {
        "id": "clean", "no": 4, "en": "Clean", "thai": "สะอาด",
        "weight": 0.15, "icon": "🧹", "mode": "auto",
        "dq_dims": ["UNIQUENESS"],
        "check": ["ไม่มีข้อมูลซ้ำ (Duplication)", "Outliers ถูกจัดการอย่างเหมาะสม"],
        "levels": {
            1: "ข้อมูลซ้ำและขยะเยอะมาก ไม่มีการจัดการใดๆ",
            2: "Clean เฉพาะตอนจะทำรายงาน หรือทำแบบนานๆ ครั้ง",
            3: "มีขั้นตอนล้างข้อมูลเป็นระบบ (คัดซ้ำ/ค่าเพี้ยนก่อนใช้)",
            4: "มีกระบวนการล้างข้อมูลอัตโนมัติทุกวัน",
            5: "ข้อมูลสะอาดตั้งแต่ต้นทางที่บันทึกเข้าระบบ",
        },
    },
    {
        "id": "connected", "no": 5, "en": "Connected", "thai": "เชื่อมโยง",
        "weight": 0.15, "icon": "🔗", "mode": "manual",
        "check": ["มี Primary/Foreign Key ชัดเจนเชื่อมโยงกันได้จริง", "Master Data มีมาตรฐานเดียวกันทั้งองค์กร"],
        "bq_source": "BigQuery ไม่มี FK enforcement — ตรวจตรงไม่ได้",
        "guidance": "แนะนำตรวจสอบ:\n• มี column _id / _key ที่ใช้ร่วมกันข้ามตารางไหม?\n• ลอง JOIN ข้ามตารางแล้วผลถูกต้องไหม?\n• Master Data (สินค้า/ลูกค้า) ใช้รหัสเดียวกันทุกระบบไหม?",
        "levels": {
            1: "ข้อมูลแยกส่วน เชื่อมหากันไม่ได้เลย (Silo)",
            2: "เชื่อมได้บางส่วน แต่ต้องให้คนมานั่งผูกเอง (VLOOKUP)",
            3: "มีรหัสกลาง (ID) ใช้เชื่อมข้ามตาราง/ระบบได้ทันที",
            4: "ทุกระบบใน BU เชื่อมถึงกันผ่าน Central Database",
            5: "ข้อมูลเชื่อมกันข้ามแผนก ดึงผ่าน API ได้ทันที",
        },
    },
    {
        "id": "timely", "no": 6, "en": "Timely", "thai": "ทันสมัย",
        "weight": 0.10, "icon": "⏱️", "mode": "auto",
        "dq_dims": [],
        "check": ["ความถี่ Update ข้อมูล (Latency) ทันต่อการใช้งาน", "ข้อมูลสดใหม่เสมอเพื่อให้ AI ตัดสินใจแม่นยำ"],
        "levels": {
            1: "อัปเดตรายเดือน หรือรอเจ้าหน้าที่ส่งไฟล์เป็นครั้งคราว",
            2: "อัปเดตรายสัปดาห์ แต่มักล่าช้ากว่าหน้างาน",
            3: "อัปเดตรายวัน (Daily) ทันรอบการตัดสินใจปกติ",
            4: "อัปเดตวันละหลายรอบ (Intra-day)",
            5: "ข้อมูลสดตลอดเวลา (Near Real-time)",
        },
    },
    {
        "id": "compliance", "no": 7, "en": "Compliance", "thai": "ถูกกฎหมาย",
        "weight": 0.05, "icon": "⚖️", "mode": "manual",
        "check": ["ปฏิบัติตาม PDPA และสิทธิ์การเข้าถึงข้อมูล", "ไม่มีความเอนเอียง (Bias) ที่ผิดจริยธรรม"],
        "bq_source": "PDPA และ Bias ไม่มีใน BigQuery metadata โดยตรง",
        "guidance": "แนะนำตรวจสอบ:\n• มีการ Mask ข้อมูลส่วนตัว (ชื่อ/เบอร์/เลขบัตร) ไหม?\n• มีการกำหนดสิทธิ์รายบุคคลใน BigQuery IAM ไหม?\n• มี Data Governance Policy ที่ชัดเจนไหม?\n• Dataset มี Bias ที่อาจทำให้ AI ตัดสินใจไม่เป็นธรรมไหม?",
        "levels": {
            1: "ใครก็เข้าถึงได้ ไม่มีระบบคุมสิทธิ์ เสี่ยง PDPA",
            2: "คุมสิทธิ์แบบกว้างๆ เช่น ใช้รหัสผ่านร่วม หรือแชร์ไดรฟ์",
            3: "ทำตาม PDPA ครบ มีปิดบังข้อมูลลับและบันทึกประวัติการใช้",
            4: "มี Data Owner ชัดเจน คุมสิทธิ์รายบุคคลตามหน้าที่",
            5: "ระบบความปลอดภัยอัตโนมัติ ตรวจสอบที่มาและสิทธิ์ได้ในคลิกเดียว",
        },
    },
    {
        "id": "contextual", "no": 8, "en": "Contextual", "thai": "AI-Ready",
        "weight": 0.10, "icon": "🤖", "mode": "auto",
        "dq_dims": [],
        "check": ["Metadata: Column description ที่ AI อ่านเข้าใจ", "Semantic Layer: นิยาม Logic ธุรกิจชัดเจน"],
        "levels": {
            1: "มีแต่ชื่อหัวตารางย่อๆ หรือรหัสที่คนนอกอ่านไม่เข้าใจ",
            2: "มีชื่อหัวตารางพอเดาได้ แต่ไม่มีเอกสารอธิบายสูตร",
            3: "มีเอกสารอธิบายสูตร KPI และนิยามแต่ละฟิลด์ชัดเจน",
            4: "มีคำอธิบายความสัมพันธ์ข้อมูลแต่ละตาราง คนนอกทำงานต่อได้",
            5: "ข้อมูลติด Tagging ครบ AI เข้าใจบริบทและวิเคราะห์ได้เอง",
        },
    },
]

READINESS = [
    (3.6, "Advanced Ready",   "พร้อมระดับสูง",     "#16a34a", "Scalable AI & Automation"),
    (2.6, "Foundation Ready", "พร้อมระดับพื้นฐาน", "#d97706", "เริ่ม Pilot / POC ได้"),
    (0.0, "Unready",          "ไม่พร้อม",           "#dc2626", "เน้นการทำ Data Cleaning ก่อน"),
]

# ============================================================
# CSS — Light Professional Theme
# ============================================================
def inject_css():
    st.markdown("""
    <style>
    /* Background */
    .stApp { background-color: #f1f5f9; }
    section[data-testid="stSidebar"] {
        background-color: #ffffff !important;
        border-right: 1px solid #e2e8f0;
    }

    /* Typography */
    h1, h2, h3, h4 { color: #0f172a !important; }
    p, li, label { color: #475569; }
    .stMarkdown p { color: #475569; }

    /* Sidebar text */
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] label,
    section[data-testid="stSidebar"] .stMarkdown { color: #334155 !important; }
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3 { color: #0f172a !important; }

    /* Input fields */
    .stTextInput input, .stSelectbox select {
        background: #ffffff !important;
        color: #0f172a !important;
        border: 1px solid #cbd5e1 !important;
        border-radius: 8px !important;
    }

    /* Buttons */
    .stButton > button[kind="primary"] {
        background: #1e40af !important;
        color: white !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
    }
    .stButton > button[kind="secondary"] {
        background: #ffffff !important;
        color: #1e40af !important;
        border: 1px solid #1e40af !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
    }

    /* Metrics */
    div[data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 12px 16px;
    }
    div[data-testid="stMetricValue"] { color: #1e40af !important; font-weight: 700 !important; }
    div[data-testid="stMetricLabel"] { color: #64748b !important; }

    /* Expander */
    .streamlit-expanderHeader { color: #1e293b !important; }
    div[data-testid="stExpander"] {
        background: #ffffff;
        border: 1px solid #e2e8f0 !important;
        border-radius: 10px;
    }

    /* Dataframe */
    .stDataFrame { border-radius: 8px; overflow: hidden; }

    /* Divider */
    hr { border-color: #e2e8f0 !important; }

    /* Radio */
    .stRadio label { color: #334155 !important; }

    /* Slider */
    .stSlider label { color: #334155 !important; }
    </style>
    """, unsafe_allow_html=True)


# ============================================================
# HELPERS
# ============================================================
def pass_rate_to_score(pct: float) -> int:
    for threshold, score in [(95,5),(85,4),(70,3),(50,2),(0,1)]:
        if pct >= threshold:
            return score
    return 1


def lag_hours_to_score(hours: float) -> int:
    for threshold, score in [(1,5),(24,4),(168,3),(720,2)]:
        if hours <= threshold:
            return score
    return 1


def desc_pct_to_score(pct: float) -> int:
    for threshold, score in [(85,5),(60,4),(30,3),(10,2)]:
        if pct >= threshold:
            return score
    return 1


def score_color(score) -> str:
    if score is None: return "#94a3b8"
    if score >= 4: return "#16a34a"
    if score >= 3: return "#d97706"
    return "#dc2626"


def score_bg(score) -> str:
    if score is None: return "#f8fafc"
    if score >= 4: return "#f0fdf4"
    if score >= 3: return "#fffbeb"
    return "#fef2f2"


def score_badge(score, total=5) -> str:
    if score is None:
        return "<span style='background:#f1f5f9;color:#94a3b8;padding:4px 10px;border-radius:20px;font-size:0.85em;'>N/A</span>"
    c = score_color(score)
    bg = score_bg(score)
    return f"<span style='background:{bg};color:{c};border:1px solid {c};padding:4px 14px;border-radius:20px;font-weight:700;font-size:1em;'>{score}/{total}</span>"


def get_readiness(ws: float):
    for threshold, label, thai, color, action in READINESS:
        if ws >= threshold:
            return label, thai, color, action
    return READINESS[-1][1:]


def weighted_score(scores: dict) -> float:
    tw = sum(d["weight"] for d in DIMENSIONS if scores.get(d["id"]) is not None)
    ws = sum(d["weight"] * scores[d["id"]] for d in DIMENSIONS if scores.get(d["id"]) is not None)
    return ws / tw if tw > 0 else 0.0


def parse_data_source(v) -> dict:
    """Parse data_source → normalized dict {project, dataset, table}
    รองรับ 2 format:
      Format A (CSV/Dataplex export): {"data_source": {"table_project_id":..., "dataset_id":..., "table_id":...}}
      Format B (live BQ STRUCT):      {"bigquery_table": {"project_id":..., "dataset_id":..., "table_id":...}}
    """
    # Step 1: parse to Python dict
    if isinstance(v, dict):
        d = v
    else:
        s = str(v)
        try:
            d = json.loads(s)
        except Exception:
            try:
                d = ast.literal_eval(s)
            except Exception:
                return {"project": "", "dataset": "", "table": ""}

    # Step 2: extract fields based on format
    if "bigquery_table" in d:
        # Format B: live BQ STRUCT
        bq = d["bigquery_table"] if isinstance(d["bigquery_table"], dict) else {}
        return {
            "project": bq.get("project_id", ""),
            "dataset": bq.get("dataset_id", ""),
            "table":   bq.get("table_id", ""),
        }

    if "data_source" in d:
        # Format A: Dataplex CSV export
        ds = d["data_source"] if isinstance(d["data_source"], dict) else {}
        return {
            "project": ds.get("table_project_id", ds.get("project_id", "")),
            "dataset": ds.get("dataset_id", ""),
            "table":   ds.get("table_id", ""),
        }

    # Fallback: flat dict
    return {
        "project": d.get("table_project_id", d.get("project_id", "")),
        "dataset": d.get("dataset_id", ""),
        "table":   d.get("table_id", ""),
    }


def parse_tables(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    seen = set()
    for v in df["data_source"]:
        try:
            parsed = parse_data_source(v)
            key = (parsed.get("project",""), parsed.get("dataset",""), parsed.get("table",""))
            if key not in seen and any(k for k in key):
                seen.add(key)
                rows.append({"project": key[0], "dataset": key[1], "table": key[2]})
        except:
            pass
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["project","dataset","table"])


# ============================================================
# BIGQUERY
# ============================================================
def bq_connect(project_id: str, creds_json: str = None, oauth_creds=None):
    try:
        from google.cloud import bigquery
        if oauth_creds:
            # Google OAuth — ใช้สิทธิ์ตัวเอง
            return bigquery.Client(project=project_id, credentials=oauth_creds), None
        if creds_json:
            # Service Account JSON
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(json.loads(creds_json))
            return bigquery.Client(project=project_id, credentials=creds), None
        # Application Default Credentials (local dev)
        return bigquery.Client(project=project_id), None
    except Exception as e:
        return None, str(e)


def bq_query(client, sql: str) -> pd.DataFrame:
    return client.query(sql).to_dataframe()


def load_dq_results(client, project_id, dataset_filter=None, table_filter=None) -> pd.DataFrame:
    where = []
    if dataset_filter:
        where.append(f"JSON_VALUE(data_source, '$.bigquery_table.dataset_id') = '{dataset_filter}'")
    if table_filter:
        where.append(f"JSON_VALUE(data_source, '$.bigquery_table.table_id') = '{table_filter}'")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    return bq_query(client, f"""
        SELECT data_source, rule_dimension, rule_name, rule_type, rule_column,
               rule_passed, rule_rows_evaluated, rule_rows_passed, rule_rows_passed_percent,
               job_start_time, last_updated
        FROM `{project_id}.data_quality.dq_result` {w}
        ORDER BY job_start_time DESC
    """)


# ============================================================
# ANALYSIS FUNCTIONS
# ============================================================

def analyze_dq_dim(df: pd.DataFrame, dq_dims: list) -> dict:
    """วิเคราะห์ pass rate จาก dq_result สำหรับ dimension ที่ระบุ"""
    sub = df[df["rule_dimension"].isin(dq_dims)].copy()
    if sub.empty:
        return {"has_data": False, "pass_rate": None, "score": None, "rule_count": 0, "per_dim": {}, "per_table": pd.DataFrame()}

    sub2 = sub.copy()
    table_names = []
    for v in sub2["data_source"]:
        try:
            p = parse_data_source(v)
            table_names.append(p.get("table", "unknown") or "unknown")
        except:
            table_names.append("unknown")
    sub2["table_name"] = table_names

    per_table = (sub2.groupby("table_name")
        .agg(total_rows=("rule_rows_evaluated","sum"),
             passed_rows=("rule_rows_passed","sum"),
             rule_count=("rule_passed","count"),
             passed_rules=("rule_passed","sum"))
        .reset_index())
    per_table["pass_rate"] = (per_table["passed_rows"] / per_table["total_rows"].replace(0, float("nan")) * 100).round(1)

    per_dim = {}
    for d in dq_dims:
        s = sub[sub["rule_dimension"] == d]
        if not s.empty:
            total = s["rule_rows_evaluated"].sum()
            pr = s["rule_rows_passed"].sum() / total * 100 if total > 0 else s["rule_rows_passed_percent"].mean()
            per_dim[d] = {"pass_rate": round(pr, 1), "rule_count": len(s)}

    total_rows = sub["rule_rows_evaluated"].sum()
    overall_pr = sub["rule_rows_passed"].sum() / total_rows * 100 if total_rows > 0 else sub["rule_rows_passed_percent"].mean()

    return {
        "has_data": True,
        "pass_rate": round(overall_pr, 1),
        "score": pass_rate_to_score(overall_pr),
        "rule_count": len(sub),
        "table_count": sub2["table_name"].nunique(),
        "per_dim": per_dim,
        "per_table": per_table,
    }


def analyze_sufficient(client, tables_df: pd.DataFrame, table_name: str, date_col: str) -> dict:
    """Query MIN/MAX(date_col) + COUNT(*) ของตารางที่เลือก"""
    row = tables_df[tables_df["table"] == table_name]
    if row.empty:
        return {"success": False, "error": f"ไม่พบตาราง '{table_name}' ใน tables_df"}
    r = row.iloc[0]
    try:
        res = bq_query(client, f"""
            SELECT
                COUNT(*) AS total_rows,
                MIN({date_col}) AS oldest_date,
                MAX({date_col}) AS newest_date,
                DATE_DIFF(CAST(MAX({date_col}) AS DATE), CAST(MIN({date_col}) AS DATE), MONTH) AS history_months
            FROM `{r['project']}.{r['dataset']}.{r['table']}`
        """)
        months = int(res["history_months"].iloc[0] or 0)
        if months < 3:       suggested = 1
        elif months < 6:     suggested = 2
        elif months < 24:    suggested = 3
        elif months < 36:    suggested = 4
        else:                suggested = 5
        return {
            "success": True,
            "total_rows": int(res["total_rows"].iloc[0]),
            "oldest_date": str(res["oldest_date"].iloc[0]),
            "newest_date": str(res["newest_date"].iloc[0]),
            "history_months": months,
            "suggested_score": suggested,
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:300]}


def analyze_timely(client, tables_df: pd.DataFrame) -> dict:
    """ตรวจ MAX(last_update_process) ในแต่ละตาราง"""
    results = []
    for _, row in tables_df.iterrows():
        try:
            res = bq_query(client, f"SELECT MAX(last_update_process) AS last_update FROM `{row['project']}.{row['dataset']}.{row['table']}`")
            lu = pd.to_datetime(res["last_update"].iloc[0])
            if lu is not None and not pd.isnull(lu):
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                lag = (datetime.now(timezone.utc) - lu).total_seconds() / 3600
                results.append({"table": row["table"], "last_update": lu.strftime("%Y-%m-%d %H:%M"), "lag_hours": round(lag, 1), "status": "ok"})
            else:
                results.append({"table": row["table"], "last_update": "NULL", "lag_hours": None, "status": "null"})
        except Exception as e:
            col_err = "no_column" if "Unrecognized name" in str(e) or "not found" in str(e).lower() else "error"
            results.append({"table": row["table"], "last_update": "—", "lag_hours": None, "status": col_err, "error": str(e)[:80]})

    if not results:
        return {"has_data": False, "score": None, "rows": pd.DataFrame()}

    df_r = pd.DataFrame(results)
    valid = df_r[df_r["status"] == "ok"]
    if valid.empty:
        return {"has_data": False, "score": 0, "rows": df_r,
                "note": "ไม่พบ column 'last_update_process' ในตารางที่ตรวจ"}

    avg_lag = valid["lag_hours"].mean()
    return {
        "has_data": True,
        "score": lag_hours_to_score(avg_lag),
        "avg_lag_hours": round(avg_lag, 1),
        "rows": df_r,
    }


def analyze_contextual(client, tables_df: pd.DataFrame) -> dict:
    """ตรวจ description fill rate จาก INFORMATION_SCHEMA"""
    all_rows = []
    for dataset in tables_df["dataset"].unique():
        project = tables_df[tables_df["dataset"] == dataset]["project"].iloc[0]
        tbl_list = "', '".join(tables_df[tables_df["dataset"] == dataset]["table"].tolist())
        try:
            res = bq_query(client, f"""
                SELECT table_name,
                    COUNTIF(description IS NOT NULL AND TRIM(description) != '') AS described,
                    COUNT(*) AS total,
                    ROUND(COUNTIF(description IS NOT NULL AND TRIM(description) != '') / COUNT(*) * 100, 1) AS desc_pct
                FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
                WHERE table_name IN ('{tbl_list}')
                GROUP BY table_name ORDER BY table_name
            """)
            all_rows.append(res)
        except Exception as e:
            pass

    if not all_rows:
        return {"has_data": False, "score": None,
                "note": f"ไม่สามารถดึง INFORMATION_SCHEMA ได้ (ตรวจ {len(tables_df)} ตาราง)"}

    df_c = pd.concat(all_rows)
    total_desc = df_c["described"].sum()
    total_cols = df_c["total"].sum()
    overall_pct = round(total_desc / total_cols * 100, 1) if total_cols > 0 else 0

    return {
        "has_data": True,
        "desc_pct": overall_pct,
        "score": desc_pct_to_score(overall_pct),
        "total_cols": int(total_cols),
        "described_cols": int(total_desc),
        "per_table": df_c,
    }


def get_date_columns(client, project: str, dataset: str, table: str) -> list:
    """ดึงรายชื่อ column ที่เป็น DATE/DATETIME/TIMESTAMP จาก INFORMATION_SCHEMA.COLUMNS"""
    try:
        res = bq_query(client, f"""
            SELECT column_name
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name = '{table}'
              AND data_type IN ('DATE', 'DATETIME', 'TIMESTAMP', 'TIMESTAMP WITH TIME ZONE')
            ORDER BY ordinal_position
        """)
        return res["column_name"].tolist()
    except Exception:
        return []


# ============================================================
# UI CARD COMPONENTS
# ============================================================

def card_header(no, icon, en, thai, weight, score, mode_label, mode_color):
    c = score_color(score)
    bg = score_bg(score)
    badge = score_badge(score)
    return f"""
    <div style="display:flex;justify-content:space-between;align-items:center;
                margin-bottom:12px;flex-wrap:wrap;gap:8px;">
        <div style="display:flex;align-items:center;gap:10px;">
            <div style="background:#1e40af;color:white;border-radius:50%;
                        width:32px;height:32px;display:flex;align-items:center;
                        justify-content:center;font-weight:700;font-size:0.9em;flex-shrink:0;">{no}</div>
            <div>
                <span style="font-size:1.1em;font-weight:700;color:#0f172a;">{icon} {en}</span>
                <span style="color:#64748b;font-size:0.88em;margin-left:6px;">({thai})</span>
            </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
            <span style="background:{mode_color}20;color:{mode_color};border:1px solid {mode_color}40;
                         padding:2px 10px;border-radius:20px;font-size:0.75em;font-weight:600;">{mode_label}</span>
            <span style="color:#64748b;font-size:0.8em;">น้ำหนัก {int(weight*100)}%</span>
            {badge}
        </div>
    </div>"""


def show_rubric(levels, current_score):
    st.markdown("**เกณฑ์การประเมิน:**")
    for lvl, desc in levels.items():
        c = score_color(lvl)
        is_current = lvl == current_score
        border = f"2px solid {c}" if is_current else "1px solid #e2e8f0"
        bg = score_bg(lvl) if is_current else "#ffffff"
        marker = f"✦ ระดับ {lvl}" if is_current else f"ระดับ {lvl}"
        fw = "700" if is_current else "400"
        st.markdown(f"""
        <div style="border:{border};background:{bg};border-radius:6px;
                    padding:6px 12px;margin:3px 0;color:#334155;font-size:0.88em;font-weight:{fw};">
            <span style="color:{c};font-weight:600;">{marker}:</span> {desc}
        </div>""", unsafe_allow_html=True)


def show_per_table_dq(per_table: pd.DataFrame):
    display = per_table.copy()
    display.columns = ["ตาราง", "Rows ตรวจ", "Rows ผ่าน", "Rules ทั้งหมด", "Rules ผ่าน", "Pass Rate (%)"]
    st.dataframe(display, use_container_width=True, hide_index=True)


# ============================================================
# DIMENSION PANELS
# ============================================================

def panel_auto(dim: dict, result: dict):
    """Panel สำหรับ Auto dimension จาก dq_result"""
    score = result.get("score")
    mode_label, mode_color = "🔵 Auto จาก BQ", "#1e40af"

    with st.expander(f"{dim['icon']} **{dim['no']}. {dim['en']}** ({dim['thai']})", expanded=True):
        st.markdown(card_header(dim["no"], dim["icon"], dim["en"], dim["thai"],
                                dim["weight"], score, mode_label, mode_color), unsafe_allow_html=True)

        # What we check
        st.markdown("**ตรวจสอบ:** " + " | ".join(dim["check"]))

        if not result.get("has_data"):
            st.warning(f"⚠️ ไม่พบข้อมูล DQ Rules สำหรับ dimension: {', '.join(dim.get('dq_dims', []))}")
            st.info("ระบบจะข้ามมิตินี้ในการคำนวณ weighted score")
            return None

        # Summary metrics
        cols = st.columns(4)
        cols[0].metric("Pass Rate รวม", f"{result['pass_rate']:.1f}%")
        cols[1].metric("คะแนน", f"{score}/5")
        cols[2].metric("Rules ทั้งหมด", result["rule_count"])
        cols[3].metric("Tables ที่ตรวจ", result.get("table_count", "—"))

        # Per DQ dimension breakdown (ถ้ามีหลาย dim เช่น Quality)
        if len(result["per_dim"]) > 1:
            st.markdown("**แยกตาม DQ Dimension:**")
            rows = [{"DQ Dimension": k, "Pass Rate (%)": f"{v['pass_rate']:.1f}%",
                     "Rules": v["rule_count"]} for k, v in result["per_dim"].items()]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # Per table
        if not result["per_table"].empty:
            with st.expander("📋 รายละเอียดตามตาราง"):
                show_per_table_dq(result["per_table"])

        # Rubric
        st.divider()
        show_rubric(dim["levels"], score)
        return score


def panel_auto_timely(dim: dict, result: dict):
    """Panel สำหรับ Timely"""
    score = result.get("score")
    mode_label, mode_color = "🔵 Auto จาก last_update_process", "#1e40af"

    with st.expander(f"{dim['icon']} **{dim['no']}. {dim['en']}** ({dim['thai']})", expanded=True):
        st.markdown(card_header(dim["no"], dim["icon"], dim["en"], dim["thai"],
                                dim["weight"], score, mode_label, mode_color), unsafe_allow_html=True)
        st.markdown("**ตรวจสอบ:** " + " | ".join(dim["check"]))
        st.caption("วิธี: Query `MAX(last_update_process)` ของแต่ละตารางใน dq_result แล้วคำนวณ lag จากเวลาปัจจุบัน")

        if not result.get("has_data"):
            st.warning(result.get("note", "ไม่สามารถคำนวณได้"))
        else:
            cols = st.columns(3)
            lag = result.get("avg_lag_hours", 0)
            lag_text = f"{lag:.1f} ชั่วโมง" if lag < 48 else f"{lag/24:.1f} วัน"
            cols[0].metric("Avg Lag", lag_text)
            cols[1].metric("คะแนน", f"{score}/5")
            cols[2].metric("Tables ที่ตรวจ", len(result["rows"]))

        # Table detail
        if not result["rows"].empty:
            rows_df = result["rows"].copy()
            status_map = {"ok": "✅ พบค่า", "null": "⚠️ ค่าเป็น NULL", "no_column": "❌ ไม่มี column", "error": "❌ Error"}
            rows_df["สถานะ"] = rows_df["status"].map(status_map)
            display = rows_df[["table", "last_update", "lag_hours", "สถานะ"]].copy()
            display.columns = ["ตาราง", "last_update_process ล่าสุด", "Lag (ชั่วโมง)", "สถานะ"]
            st.dataframe(display, use_container_width=True, hide_index=True)

        st.divider()
        show_rubric(dim["levels"], score)
        return score


def panel_auto_contextual(dim: dict, result: dict):
    """Panel สำหรับ Contextual"""
    score = result.get("score")
    mode_label, mode_color = "🔵 Auto จาก INFORMATION_SCHEMA", "#1e40af"

    with st.expander(f"{dim['icon']} **{dim['no']}. {dim['en']}** ({dim['thai']})", expanded=True):
        st.markdown(card_header(dim["no"], dim["icon"], dim["en"], dim["thai"],
                                dim["weight"], score, mode_label, mode_color), unsafe_allow_html=True)
        st.markdown("**ตรวจสอบ:** " + " | ".join(dim["check"]))
        st.caption("วิธี: นับ % columns ที่มี description ใน INFORMATION_SCHEMA.COLUMN_FIELD_PATHS")

        if not result.get("has_data"):
            note = result.get("note", "ไม่สามารถดึง INFORMATION_SCHEMA ได้")
            if not client:
                st.warning(f"⚠️ Demo Mode — {note}\n\nต้องเชื่อมต่อ BigQuery จริงจึงจะ query INFORMATION_SCHEMA ได้")
            else:
                st.warning(f"⚠️ {note}")
        else:
            cols = st.columns(4)
            cols[0].metric("Description Fill Rate", f"{result['desc_pct']:.1f}%")
            cols[1].metric("คะแนน", f"{score}/5")
            cols[2].metric("Columns มี description", result["described_cols"])
            cols[3].metric("Total Columns", result["total_cols"])

            if not result["per_table"].empty:
                with st.expander("📋 รายละเอียดตามตาราง"):
                    display = result["per_table"].copy()
                    display.columns = ["ตาราง", "มี Description", "Columns ทั้งหมด", "Fill Rate (%)"]
                    st.dataframe(display, use_container_width=True, hide_index=True)

        st.divider()
        show_rubric(dim["levels"], score)
        return score


def panel_sufficient(dim: dict, tables_df: pd.DataFrame, client) -> int:
    """Panel สำหรับ Sufficient — semi-auto: เลือกตาราง + date column แล้ว query"""
    key_score = "score_sufficient"
    if key_score not in st.session_state:
        st.session_state[key_score] = 3
    current = st.session_state[key_score]
    mode_label = "🟡 Semi-Auto (ระบุ date column)" if client else "✏️ Demo — ประเมินด้วยตนเอง"
    mode_color = "#d97706" if client else "#7c3aed"

    with st.expander(f"{dim['icon']} **{dim['no']}. {dim['en']}** ({dim['thai']})", expanded=True):
        st.markdown(card_header(dim["no"], dim["icon"], dim["en"], dim["thai"],
                                dim["weight"], current, mode_label, mode_color), unsafe_allow_html=True)
        st.markdown("**ตรวจสอบ:** " + " | ".join(dim["check"]))

        # แสดงจำนวน rows จาก dq_result เป็น reference
        if not tables_df.empty:
            st.info(f"📊 ตรวจพบ **{len(tables_df)} ตาราง** จาก dq_result: {', '.join(tables_df['table'].tolist())}")

        if client and not tables_df.empty:
            st.markdown("**ระบุข้อมูลเพื่อ Query ช่วงวันที่:**")
            c1, c2, c3 = st.columns([2, 2, 1])
            with c1:
                sel_table = st.selectbox("เลือกตาราง", tables_df["table"].tolist(), key="suf_table")

            # Auto-load date columns when table is selected (cached per table)
            cache_key = f"__date_cols__{sel_table}"
            if cache_key not in st.session_state:
                with st.spinner(f"กำลังโหลด date columns ของ {sel_table}..."):
                    row = tables_df[tables_df["table"] == sel_table].iloc[0]
                    found_cols = get_date_columns(client, row["project"], row["dataset"], sel_table)
                    st.session_state[cache_key] = found_cols

            date_col_options = st.session_state.get(cache_key, [])

            with c2:
                if date_col_options:
                    date_col = st.selectbox(
                        f"เลือก Date Column (พบ {len(date_col_options)} column)",
                        date_col_options,
                        key="suf_date_col_select",
                    )
                else:
                    st.caption("⚠️ ไม่พบ DATE/DATETIME/TIMESTAMP column อัตโนมัติ — พิมพ์เอง")
                    date_col = st.text_input(
                        "ชื่อ Date Column",
                        placeholder="เช่น txn_date, created_date",
                        key="suf_date_col",
                    )
            with c3:
                st.markdown("<br>", unsafe_allow_html=True)
                check_btn = st.button("🔍 ตรวจสอบ", key="suf_check_btn")

            if check_btn:
                if not date_col:
                    st.warning("⚠️ กรุณาระบุชื่อ Date Column")
                else:
                    with st.spinner("กำลัง query..."):
                        result = analyze_sufficient(client, tables_df, sel_table, date_col)
                    st.session_state["suf_result"] = result

            result = st.session_state.get("suf_result")
            if result:
                if result["success"]:
                    months = result["history_months"]
                    months_text = f"{months} เดือน" if months < 24 else f"{months//12} ปี {months%12} เดือน"
                    rc1, rc2, rc3, rc4 = st.columns(4)
                    rc1.metric("Total Rows", f"{result['total_rows']:,}")
                    rc2.metric("ช่วงเวลา", months_text)
                    rc3.metric("ข้อมูลเก่าสุด", result["oldest_date"])
                    rc4.metric("ข้อมูลใหม่สุด", result["newest_date"])
                    sug = result["suggested_score"]
                    c = score_color(sug)
                    st.markdown(f"""
                    <div style="background:{score_bg(sug)};border:1px solid {c};border-radius:8px;padding:10px 16px;margin:8px 0;">
                        <b style="color:{c};">คะแนนแนะนำ: {sug}/5</b> — {dim['levels'][sug]}<br>
                        <span style="color:#64748b;font-size:0.85em;">ปรับได้จาก slider ด้านล่าง</span>
                    </div>""", unsafe_allow_html=True)
                    st.session_state[key_score] = sug
                    current = sug
                else:
                    st.error(f"❌ Query ล้มเหลว: {result['error']}")
        else:
            if not client:
                st.warning("⚠️ Demo Mode — ไม่สามารถ query ตารางจริงได้ ประเมินด้วยตนเองตาม rubric")
            st.caption("💡 เมื่อเชื่อมต่อ BigQuery จริง สามารถระบุ date column เพื่อให้ระบบ query ช่วงวันที่อัตโนมัติ")

        st.divider()
        show_rubric(dim["levels"], current)
        score = st.slider("เลือกระดับ Sufficient", min_value=1, max_value=5,
                          value=current, key="slider_sufficient")
        st.session_state[key_score] = score
        c = score_color(score)
        st.markdown(f"""
        <div style="background:{score_bg(score)};border:1px solid {c};border-radius:8px;padding:10px 16px;margin-top:8px;">
            <span style="color:{c};font-weight:700;">ระดับที่เลือก: {score}/5</span> — {dim['levels'][score]}
        </div>""", unsafe_allow_html=True)
    return score


def panel_manual(dim: dict) -> int:
    """Panel สำหรับ Manual dimension"""
    key = f"score_{dim['id']}"
    if key not in st.session_state:
        st.session_state[key] = 3
    current = st.session_state[key]
    mode_label, mode_color = "✏️ ประเมินด้วยตนเอง", "#7c3aed"

    with st.expander(f"{dim['icon']} **{dim['no']}. {dim['en']}** ({dim['thai']})", expanded=True):
        st.markdown(card_header(dim["no"], dim["icon"], dim["en"], dim["thai"],
                                dim["weight"], current, mode_label, mode_color), unsafe_allow_html=True)
        st.markdown("**ตรวจสอบ:** " + " | ".join(dim["check"]))

        # Why manual
        c1, c2 = st.columns([1, 2])
        with c1:
            st.error(f"🔒 **ทำไมต้องประเมินเอง?**\n\n{dim.get('bq_source','')}")
        with c2:
            st.info(f"💡 **คำแนะนำในการประเมิน:**\n\n{dim.get('guidance','')}")

        st.divider()
        show_rubric(dim["levels"], current)

        score = st.slider(
            f"เลือกระดับ {dim['en']}",
            min_value=1, max_value=5, value=current,
            key=f"slider_{dim['id']}",
        )
        st.session_state[key] = score

        c = score_color(score)
        st.markdown(f"""
        <div style="background:{score_bg(score)};border:1px solid {c};border-radius:8px;
                    padding:10px 16px;margin-top:8px;">
            <span style="color:{c};font-weight:700;">ระดับที่เลือก: {score}/5</span>
            — {dim['levels'][score]}
        </div>""", unsafe_allow_html=True)
    return score


# ============================================================
# CERTIFICATE
# ============================================================
def show_certificate(project_id, scores, ws):
    label, thai, color, action = get_readiness(ws)
    scan_date = datetime.now().strftime("%d %b %Y %H:%M")
    scored_dims = sum(1 for v in scores.values() if v is not None)

    # Header
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#1e3a8a 0%,#1e40af 50%,#1d4ed8 100%);
                border-radius:16px;padding:32px;text-align:center;
                box-shadow:0 4px 24px rgba(30,64,175,0.3);margin-bottom:24px;">
        <div style="font-size:48px;margin-bottom:4px;">🏆</div>
        <div style="color:#bfdbfe;font-size:0.85em;letter-spacing:4px;text-transform:uppercase;">
            AI Data Readiness Certificate</div>
        <h1 style="color:white !important;margin:4px 0;font-size:2em;font-weight:900;letter-spacing:2px;">
            BU DATA SCORE</h1>
        <div style="font-size:3.5em;font-weight:900;color:white;line-height:1.1;margin:8px 0;">
            {ws:.2f}<span style="font-size:0.4em;color:#93c5fd;"> / 5.00</span>
        </div>
        <div style="background:rgba(255,255,255,0.15);border:2px solid rgba(255,255,255,0.5);
                    border-radius:50px;padding:6px 24px;display:inline-block;margin-top:8px;">
            <span style="color:white;font-size:1.05em;font-weight:700;">{label} — {thai}</span>
        </div>
        <div style="color:#bfdbfe;margin-top:12px;font-size:0.85em;">
            Project: <b style="color:white;">{project_id}</b>
            &nbsp;|&nbsp; ประเมิน: <b style="color:white;">{scan_date}</b>
            &nbsp;|&nbsp; มิติที่ประเมิน: <b style="color:white;">{scored_dims}/8</b>
        </div>
        <div style="color:#93c5fd;margin-top:6px;font-size:0.82em;">💡 {action}</div>
    </div>""", unsafe_allow_html=True)

    # Gauge + Radar
    col1, col2 = st.columns(2)
    with col1:
        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=round(ws, 2),
            domain={"x": [0,1], "y": [0,1]},
            title={"text": "Overall BU Score", "font": {"color": "#0f172a", "size": 16}},
            number={"font": {"color": "#1e40af", "size": 60}, "suffix": " / 5"},
            gauge={
                "axis": {"range": [0,5], "dtick": 1, "tickcolor": "#94a3b8"},
                "bar": {"color": "#1e40af", "thickness": 0.3},
                "bgcolor": "#f8fafc",
                "borderwidth": 0,
                "steps": [
                    {"range": [0,2.5], "color": "rgba(220,38,38,0.1)"},
                    {"range": [2.5,3.6], "color": "rgba(217,119,6,0.1)"},
                    {"range": [3.6,5], "color": "rgba(22,163,74,0.1)"},
                ],
                "threshold": {"line": {"color": "#1e40af","width": 3}, "thickness": 0.85, "value": ws},
            }
        ))
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          height=270, margin=dict(t=40, b=0, l=20, r=20))
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        labels = [f"{d['en']}<br>({d['thai']})" for d in DIMENSIONS]
        values = [scores.get(d["id"]) or 0 for d in DIMENSIONS]
        fig2 = go.Figure(go.Scatterpolar(
            r=values + [values[0]], theta=labels + [labels[0]],
            fill="toself", fillcolor="rgba(30,64,175,0.15)",
            line=dict(color="#1e40af", width=2),
        ))
        fig2.update_layout(
            polar=dict(
                radialaxis=dict(visible=True, range=[0,5], dtick=1, color="#94a3b8"),
                angularaxis=dict(color="#475569"),
                bgcolor="rgba(248,250,252,0.8)",
            ),
            paper_bgcolor="rgba(0,0,0,0)", font={"color": "#475569", "size": 10},
            showlegend=False, height=300, margin=dict(t=10, b=10, l=30, r=30),
        )
        st.plotly_chart(fig2, use_container_width=True)

    # Summary table
    st.markdown("### 📊 ตารางสรุปคะแนน")
    rows = []
    for d in DIMENSIONS:
        s = scores.get(d["id"])
        level_txt = d["levels"][s] if s else "—"
        mode_txt = "🔵 Auto" if d["mode"] == "auto" else "✏️ Manual"
        rows.append({
            "มิติ": f"{d['icon']} {d['en']} ({d['thai']})",
            "คะแนน": f"{s}/5" if s else "N/A",
            "ระดับ": level_txt,
            "Weight": f"{int(d['weight']*100)}%",
            "Weighted": f"{(s or 0)*d['weight']:.3f}",
            "Mode": mode_txt,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ============================================================
# MAIN
# ============================================================
def main():
    inject_css()

    # ---- OAuth callback ต้องดักก่อน render อื่นๆ ----
    if handle_oauth_callback():
        st.rerun()

    # Init session state
    for d in DIMENSIONS:
        if f"score_{d['id']}" not in st.session_state:
            st.session_state[f"score_{d['id']}"] = 3

    # ---- SIDEBAR ----
    with st.sidebar:
        st.markdown("## ⚙️ การตั้งค่า")
        st.divider()

        project_id = st.text_input("🔑 GCP Project ID", placeholder="your-project-id")
        st.divider()

        # ---- Authentication ----
        oauth_creds, oauth_email = sidebar_oauth_section()
        creds_json = None

        if oauth_creds is None:
            # OAuth ไม่ได้ตั้งค่า หรือยังไม่ login → ใช้วิธีเดิม
            if _oauth_config() is None:
                # ไม่มี secrets → แสดงตัวเลือก SA key / ADC
                auth = st.radio("Authentication",
                    ["Application Default Credentials (ADC)", "Service Account Key File"])
                if auth == "Service Account Key File":
                    f = st.file_uploader("อัปโหลด JSON Key", type=["json"])
                    if f:
                        creds_json = f.read().decode("utf-8")
                        st.success("✅ อัปโหลดสำเร็จ")

        st.divider()
        st.markdown("**🔎 Filter (optional)**")
        ds_filter = st.text_input("Dataset", placeholder="เช่น PIGPOS")
        tbl_filter = st.text_input("Table", placeholder="เช่น FR_MS_TRN_FEED")

        st.divider()
        load_btn = st.button("📡 โหลดข้อมูลจาก BigQuery", use_container_width=True, type="primary")
        demo_btn = st.button("🧪 Demo Mode (CSV)", use_container_width=True)
        calc_btn = st.button("🚀 แสดงผล BU Score", use_container_width=True, type="primary")

        st.divider()
        st.markdown("""
**📖 โหมดการคำนวณ**
- 🔵 **Auto** — คำนวณจาก BigQuery อัตโนมัติ
- ✏️ **Manual** — ประเมินด้วยตนเองพร้อมคำแนะนำ

**📊 เกณฑ์คะแนน (Auto)**
| Pass Rate | คะแนน |
|-----------|-------|
| ≥ 95% | 5 |
| ≥ 85% | 4 |
| ≥ 70% | 3 |
| ≥ 50% | 2 |
| < 50% | 1 |
        """)

    # ---- LOAD DATA ----
    if load_btn:
        if not project_id:
            st.warning("⚠️ กรุณากรอก GCP Project ID")
        elif oauth_creds is None and creds_json is None and _oauth_config() is None:
            st.warning("⚠️ กรุณา Login with Google หรืออัปโหลด JSON Key ก่อน")
        else:
            with st.spinner("กำลังเชื่อมต่อ BigQuery..."):
                client, err = bq_connect(project_id, creds_json, oauth_creds)
            if err:
                st.error(f"❌ เชื่อมต่อไม่สำเร็จ: {err}")
            else:
                with st.spinner("กำลังโหลด DQ Results..."):
                    try:
                        df = load_dq_results(client, project_id, ds_filter or None, tbl_filter or None)
                        st.session_state["df"] = df
                        st.session_state["client"] = client
                        st.session_state["project"] = project_id
                        who = oauth_email or project_id
                        st.success(f"✅ โหลดสำเร็จ — {len(df):,} rules ({who})")
                    except Exception as e:
                        st.error(f"❌ {e}")

    if demo_btn:
        try:
            df = pd.read_csv(os.path.join(BASE_DIR, "data_quality.dq_result.csv"), encoding="utf-8-sig")
            st.session_state["df"] = df
            st.session_state["client"] = None
            st.session_state["project"] = "cpf-farm-th (Demo)"
            st.success(f"✅ โหลด Demo — {len(df):,} rules")
        except Exception as e:
            st.error(f"ไม่พบไฟล์ CSV: {e}")

    df = st.session_state.get("df")
    client = st.session_state.get("client")
    project = st.session_state.get("project", project_id or "")

    # ---- WELCOME ----
    if df is None:
        st.markdown("""
        <div style="text-align:center;padding:60px 20px;">
            <div style="font-size:64px;">🏆</div>
            <h1 style="color:#0f172a;">BU Data Score</h1>
            <p style="color:#64748b;max-width:480px;margin:16px auto;font-size:1.05em;">
                ประเมินความพร้อมของข้อมูลสำหรับ AI <strong>8 มิติ</strong><br>
                โหลดข้อมูลจาก BigQuery แล้วประเมินคะแนนแต่ละมิติ
            </p>
        </div>""", unsafe_allow_html=True)
        return

    # ---- TABS ----
    tab1, tab2 = st.tabs(["📋 ประเมิน 8 มิติ", "🏆 ผลลัพธ์ BU Score"])

    # ============ TAB 1: Assessment ============
    with tab1:
        st.markdown(f"### 📋 ประเมิน 8 มิติ — Project: `{project}`")
        st.caption(f"โหลดข้อมูลแล้ว: {len(df):,} rules | DQ Dimensions ที่พบ: {', '.join(df['rule_dimension'].unique())}")

        tables_df = parse_tables(df)
        scores = {}

        for dim in DIMENSIONS:
            if dim["id"] == "sufficient":
                scores["sufficient"] = panel_sufficient(dim, tables_df, client)

            elif dim["mode"] == "auto":
                if dim["id"] == "timely":
                    if client:
                        with st.spinner("กำลังตรวจ Timely..."):
                            result = analyze_timely(client, tables_df)
                    else:
                        result = {"has_data": False, "rows": pd.DataFrame(),
                                  "note": "Demo mode — ไม่สามารถ query ตารางจริงได้"}
                    scores["timely"] = panel_auto_timely(dim, result)

                elif dim["id"] == "contextual":
                    if client:
                        with st.spinner("กำลังตรวจ Contextual (INFORMATION_SCHEMA)..."):
                            result = analyze_contextual(client, tables_df)
                    else:
                        result = {"has_data": False, "note": "ต้องเชื่อมต่อ BigQuery จริง"}
                    scores["contextual"] = panel_auto_contextual(dim, result)

                else:
                    result = analyze_dq_dim(df, dim["dq_dims"])
                    scores[dim["id"]] = panel_auto(dim, result)

            else:  # manual
                scores[dim["id"]] = panel_manual(dim)

        st.session_state["scores"] = scores

    # ============ TAB 2: Result ============
    with tab2:
        scores = st.session_state.get("scores", {d["id"]: st.session_state.get(f"score_{d['id']}", 3) for d in DIMENSIONS})
        ws = weighted_score(scores)

        if ws == 0:
            st.info("ไปที่แท็บ 'ประเมิน 8 มิติ' ก่อน แล้วกด '🚀 แสดงผล BU Score'")
        else:
            show_certificate(project, scores, ws)

    # Calc button in sidebar triggers tab2 scroll
    if calc_btn:
        scores = {d["id"]: st.session_state.get(f"score_{d['id']}") for d in DIMENSIONS}
        st.session_state["scores"] = scores


if __name__ == "__main__":
    main()
