import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import os
import time
import base64
import hashlib
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from collections import Counter

# =========================================================
# [0] 기본 설정 및 상태 초기화
# =========================================================
st.set_page_config(
    page_title="국어활동 AI 분석기", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 세션 상태 초기화
if 'step' not in st.session_state: st.session_state.step = 0
if 'mode_key' not in st.session_state: st.session_state.mode_key = None
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'file_bytes' not in st.session_state: st.session_state.file_bytes = None
if 'file_type' not in st.session_state: st.session_state.file_type = None
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'start_offset' not in st.session_state: st.session_state.start_offset = 1
if 'extracted_text' not in st.session_state: st.session_state.extracted_text = ""
if 'debug_mode' not in st.session_state: st.session_state.debug_mode = False

# =========================================================
# [1] 디자인: 그라데이션 카드 버튼 (CSS 강제 적용)
# =========================================================
if st.session_state.step == 0:
    st.markdown("""
        <style>
            /* 기본 폰트 */
            .stTextArea textarea { font-family: 'Malgun Gothic', sans-serif !important; }
            
            /* 1. 버튼을 카드처럼 변신시키기 */
            div.stButton > button {
                width: 100%;
                height: 260px; /* 카드 높이 확보 */
                border-radius: 20px !important;
                border: 0px solid transparent !important;
                color: white !important;
                transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
                box-shadow: 0 4px 15px rgba(0,0,0,0.3) !important;
                font-size: 1.1rem !important;
                font-weight: 500 !important;
                white-space: pre-wrap; /* 줄바꿈 허용 */
                line-height: 1.6 !important;
                padding: 20px !important;
            }
            
            /* 2. 호버 효과: 둥실 떠오르며 밝아짐 */
            div.stButton > button:hover {
                transform: translateY(-8px) scale(1.01);
                box-shadow: 0 15px 35px rgba(0,0,0,0.5) !important;
                filter: brightness(1.1);
            }

            /* 3. 색상 테마 적용 (CSS 선택자 순서 중요) */
            
            /* 첫 번째 컬럼: 대한민국 (오션 블루) */
            div[data-testid="column"]:nth-of-type(1) div.stButton > button {
                background: linear-gradient(145deg, #1e3a8a 0%, #0369a1 60%, #06b6d4 100%) !important;
            }
            
            /* 두 번째 컬럼: 북한 (로즈 와인) */
            div[data-testid="column"]:nth-of-type(2) div.stButton > button {
                background: linear-gradient(145deg, #881337 0%, #be123c 60%, #fb7185 100%) !important;
            }
            
            /* 세 번째 컬럼: 관리자 (다크 글래스) */
            div[data-testid="column"]:nth-of-type(3) div.stButton > button {
                background: linear-gradient(145deg, #1f2937 0%, #374151 100%) !important;
                border: 1px solid #555 !important;
                color: #ccc !important;
            }
        </style>
    """, unsafe_allow_html=True)
else:
    # [Step 1~4] 작업 단계용 깔끔한 스타일
    st.markdown("""
        <style>
            .stTextArea textarea { font-family: 'Malgun Gothic', sans-serif !important; font-size: 16px !important; line-height: 1.6 !important; }
            .stButton button { border-radius: 8px; font-weight: bold; height: auto; }
            
            /* 진행바 스타일 */
            .progress-box { display: flex; justify-content: space-between; margin: 20px 0 40px 0; border-bottom: 1px solid #444; padding-bottom: 10px; }
            .step-item { color: #666; font-size: 0.9rem; font-weight: 500; }
            .step-active { color: #4CAF50; font-weight: 700; border-bottom: 3px solid #4CAF50; padding-bottom: 7px; }
        </style>
    """, unsafe_allow_html=True)

# 라이브러리 로드 확인
try:
    import pdfplumber
    PLUMBER_AVAILABLE = True
except ImportError:
    PLUMBER_AVAILABLE = False
try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

# =========================================================
# [2] API 및 유틸리티
# =========================================================
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = ""
except:
    API_KEY = ""

MODEL_NAME = "gemini-2.0-flash-exp"
SHEET_NAME = "Korean_DB"

@st.cache_resource
def get_google_sheet_client():
    try:
        if "gcp_service_account" not in st.secrets: return None
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict: creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        return gspread.authorize(creds)
    except: return None

def get_sheet_data_fresh(mode_key):
    client = get_google_sheet_client()
    if not client: return None, []
    target = "South_Korea" if mode_key == "SOUTH" else "North_Korea"
    try:
        sh = client.open(SHEET_NAME)
        try: ws = sh.worksheet(target)
        except: 
            ws = sh.add_worksheet(title=target, rows=1000, cols=20)
            ws.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context"])
        return ws, ws.get_all_records()
    except: return None, []

def send_data_with_retry(sheet_obj, data, is_multiple=False):
    if not sheet_obj: return False
    for _ in range(3):
        try:
            if is_multiple: sheet_obj.append_rows([[str(i) for i in r] for r in data])
            else: sheet_obj.append_row([str(i) for i in data])
            return True
        except: time.sleep(1)
    return False

def save_backup_to_cloud(mode_key, df):
    client = get_google_sheet_client()
    if not client or df is None or df.empty: return False
    try:
        sh = client.open(SHEET_NAME)
        ws = sh.worksheet(f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}")
        ws.clear()
        ws.update([df.fillna("").astype(str).columns.tolist()] + df.fillna("").astype(str).values.tolist())
        return True
    except: return False

# =========================================================
# [3] AI 엔진 & 데이터 로직
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    for row in sheet_data[-100:]:
        if row.get('action') == 'delete':
            rules.append(f"- [제외 권장]: '{row.get('original_word')}'는 과거 삭제 이력이 있습니다. 문맥상 불필요하면 제외하세요.")
        elif row.get('action') in ['add', 'modify']:
            rules.append(f"- [고정 규칙]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}', 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    return "\n[사용자 학습 데이터]:\n" + "\n".join(rules) + "\n" if rules else ""

def api_call_direct(prompt, image_bytes=None):
    if not API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    parts = [{"text": prompt}]
    if image_bytes:
        parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_bytes).decode('utf-8')}})
    try:
        res = requests.post(url, headers=headers, json={"contents": [{"parts": parts}]}, timeout=300)
        if res.status_code == 200: return res.json()['candidates'][0]['content']['parts'][0]['text']
        return None
    except: return None

def api_call_vision_ocr(image_bytes):
    return api_call_direct("이 이미지의 텍스트를 보이는 그대로 추출해줘. 줄바꿈 유지.", image_bytes) or ""

def split_text_smartly(text, chunk_size=1000):
    sentences = re.split(r'(?<=[.?!])\s+|\\n', text)
    chunks = []
    curr = ""
    for s in sentences:
        if len(curr) + len(s) < chunk_size: curr += s + " "
        else: chunks.append(curr.strip()); curr = s + " "
    if curr: chunks.append(curr.strip())
    return chunks

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    prompt = f"""
    당신은 '{"대한민국 표준어" if mode_key=="SOUTH" else "북한 문화어"}' 형태소 분석 전문가입니다.
    {generate_prompt_from_sheet(sheet_data)}
    
    [분석 규칙]
    1. 조사/어미 제거, '하다' 용언은 명사로 분류.
    2. 품사: 명사, 동사, 형용사, 부사, 관형사, 대명사, 고유명사.
    3. **동음이의어: 원형 뒤에 괄호로 뜻 구분 (필수).** (예: 배(과일), 배(선박))
    4. **인명/지명: 원형 뒤에 (이름)/(지명) 표기 (필수).** (예: 지혜(이름), 서울(지명))
    5. 출력: JSON 포맷.
    """
    
    if image_bytes:
        try: return json.loads(re.search(r'\[.*\]', api_call_direct(prompt + "\n(OCR 참고)", image_bytes), re.DOTALL).group())
        except: return []
    else:
        chunks = split_text_smartly(text)
        res = []
        for chunk in chunks:
            if not chunk.strip(): continue
            try:
                r = api_call_direct(prompt + f"\n[텍스트]:\n{chunk}")
                if r: res.extend(json.loads(re.search(r'\[.*\]', r, re.DOTALL).group()))
            except: pass
        return res

# [데이터 유틸리티]
def clean_val_for_save(v):
    if isinstance(v, str): 
        v = v.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('👤 ', '')
        return v.strip()
    return v

def calc_freq(row):
    total = 0
    for c in row.index:
        if str(c).startswith('쪽수'):
            v = str(row[c])
            if '_' in v: 
                try: total += int(v.split('_')[1])
                except: total += 1
            elif v not in ['nan', '', 'None']: total += 1
    return total

def merge_master_data(old_df, new_df):
    if old_df is None or old_df.empty: return new_df
    key_cols = ['자료', '구분']
    merged = pd.merge(old_df, new_df, on=key_cols, how='outer', suffixes=('_old', '_new'))
    
    page_cols_old = [c for c in old_df.columns if c.startswith('쪽수')]
    page_cols_new = [c for c in new_df.columns if c.startswith('쪽수')]
    
    final_rows = []
    for _, row in merged.iterrows():
        new_row = {k: row[k] for k in key_cols}
        pages = []
        for c in page_cols_old:
            val = row.get(f"{c}_old", row.get(c))
            if pd.notna(val) and str(val) != 'nan' and str(val) != '': pages.append(str(val))
        for c in page_cols_new:
            val = row.get(f"{c}_new", row.get(c))
            if pd.notna(val) and str(val) != 'nan' and str(val) != '': pages.append(str(val))
            
        unique_pages = sorted(list(set(pages)))
        for i, p in enumerate(unique_pages): new_row[f"쪽수{i+1}"] = p
        final_rows.append(new_row)
        
    result_df = pd.DataFrame(final_rows)
    result_df['출연횟수'] = result_df.apply(calc_freq, axis=1)
    
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    result_df['sk'] = result_df['구분'].map(sort_map).fillna(5)
    result_df = result_df.sort_values(['sk', '자료']).drop('sk', axis=1)
    return result_df

def extract_text_unified(file_bytes, file_type, page_idx):
    raw_text = ""
    if "image" in file_type: raw_text = api_call_vision_ocr(file_bytes)
    elif "pdf" in file_type:
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    if page_idx < len(pdf.pages):
                        raw_text = pdf.pages[page_idx].extract_text()
            except: pass
        if (not raw_text or len(raw_text) < 10) and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc): raw_text = doc[page_idx].get_text()
            except: pass
        if (not raw_text or len(raw_text) < 10) and FITZ_AVAILABLE:
             try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc):
                    pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                    raw_text = api_call_vision_ocr(pix.tobytes("png"))
             except: pass

    lines = raw_text.split('\n')
    cleaned = [l for l in lines if not re.search(r'\.indd|\d{4}-\d{2}-\d{2}|오후|오전', l)]
    return "\n".join(cleaned).strip()

def get_page_image(file_bytes, file_type, page_idx):
    if "image" in file_type: return file_bytes
    if "pdf" in file_type and FITZ_AVAILABLE:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if page_idx < len(doc): return doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0)).tobytes("png")
    return None

# =========================================================
# [4] UI: 메인 루프
# =========================================================
if st.session_state.step > 0:
    steps = ["1. 모드 선택", "2. 데이터 소스", "3. 자료 입력", "4. 결과 확인"]
    st.markdown('<div class="progress-box">', unsafe_allow_html=True)
    cols = st.columns(4)
    for i, (col, title) in enumerate(zip(cols, steps)):
        with col:
            cls = "step-active" if i+1 == st.session_state.step else "step-item"
            st.markdown(f'<div class="{cls}">{title}</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

# ---------------------------------------------------------
# STEP 0: 시작 화면 (안정적인 그라데이션 카드 버튼)
# ---------------------------------------------------------
if st.session_state.step == 0:
    st.markdown("<h1 style='text-align: center; margin-bottom: 20px;'>📚 국어활동 AI 분석기</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #888; margin-bottom: 50px;'>원하는 언어 규범을 선택하여 분석을 시작하세요.</p>", unsafe_allow_html=True)
    
    c1, c2, c3 = st.columns(3)
    
    # [1] 대한민국 표준어 (오션 블루)
    with c1:
        # 버튼 텍스트에 유니코드와 줄바꿈을 활용하여 '카드 안의 버튼'처럼 보이게 연출
        label_south = "🏛️\n\n대한민국 표준어\n\n(표준국어대사전 기준)\n\n\n[ 표준어 모드 입장 ➜ ]"
        if st.button(label_south, key="btn_south", use_container_width=True):
            st.session_state.mode_key = "SOUTH"
            st.session_state.step = 1
            st.toast("✅ 대한민국 학습 서버에 연결되었습니다.")
            time.sleep(0.5); st.rerun()

    # [2] 북한 문화어 (로즈 와인)
    with c2:
        label_north = "🏔️\n\n북한 문화어\n\n(문화어 규범 기준)\n\n\n[ 문화어 모드 입장 ➜ ]"
        if st.button(label_north, key="btn_north", use_container_width=True):
            st.session_state.mode_key = "NORTH"
            st.session_state.step = 1
            st.toast("✅ 북한 학습 서버에 연결되었습니다.")
            time.sleep(0.5); st.rerun()

    # [3] 관리자 모드 (다크 글래스)
    with c3:
        label_debug = "🛠️\n\n관리자 모드\n\n(시스템 로그 확인)\n\n\n[ 로그 패널 열기 ]"
        if st.button(label_debug, key="btn_debug", use_container_width=True):
            st.session_state.debug_mode = True
            st.info("로그 패널이 활성화되었습니다.")

# ---------------------------------------------------------
# STEP 1: 데이터 소스
# ---------------------------------------------------------
elif st.session_state.step == 1:
    st.header("📂 데이터 소스 선택")
    col1, col2 = st.columns(2)
    
    with col1:
        with st.container(border=True):
            st.subheader("📂 이어하기")
            st.caption("기존에 작업하던 엑셀 파일이 있다면 업로드하세요. (자동 병합)")
            up_excel = st.file_uploader("엑셀 파일 (.xlsx)", type=['xlsx'])
            if up_excel:
                try:
                    loaded = pd.read_excel(up_excel)
                    st.session_state.master_df = loaded
                    st.session_state.step = 2
                    st.toast("데이터 로드 완료! 저장 시 자동으로 합쳐집니다.")
                    time.sleep(0.5); st.rerun()
                except: st.error("올바른 엑셀 파일 형식이 아닙니다.")

    with col2:
        with st.container(border=True):
            st.subheader("🆕 새로 시작하기")
            st.caption("기존 데이터 없이 빈 상태로 분석을 시작합니다.")
            st.write("") 
            if st.button("새 프로젝트 생성", use_container_width=True):
                st.session_state.master_df = None
                st.session_state.step = 2
                st.rerun()
    
    st.markdown("---")
    if st.button("⬅️ 모드 다시 선택"): st.session_state.step = 0; st.rerun()

# ---------------------------------------------------------
# STEP 2: 자료 입력
# ---------------------------------------------------------
elif st.session_state.step == 2:
    if st.sidebar.button("🏠 처음으로 (초기화)"):
        st.session_state.clear()
        st.rerun()

    st.header("📝 분석 자료 입력")
    
    tab1, tab2 = st.tabs(["📄 파일 분석 (PDF/이미지)", "✍️ 텍스트 직접 입력"])
    
    with tab1:
        file = st.file_uploader("파일 업로드", type=['pdf', 'png', 'jpg'])
        if file:
            file_bytes = file.getvalue()
            if st.session_state.file_bytes != file_bytes:
                st.session_state.file_bytes = file_bytes
                st.session_state.file_type = file.type
                st.session_state.page_idx = 0
                st.session_state.extracted_text = extract_text_unified(file_bytes, file.type, 0)
            
            c_view, c_text = st.columns(2)
            with c_view:
                st.info("📷 원본 미리보기")
                img = get_page_image(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                if img: st.image(img, use_container_width=True)
                
                if "pdf" in st.session_state.file_type:
                    b1, b2 = st.columns(2)
                    if b1.button("◀ 이전"):
                        st.session_state.page_idx = max(0, st.session_state.page_idx - 1)
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                        st.rerun()
                    if b2.button("다음 ▶"):
                        st.session_state.page_idx += 1
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                        st.rerun()
                    st.session_state.start_offset = st.number_input("시작 쪽수 설정", value=st.session_state.start_offset)
            
            with c_text:
                st.info("✍️ 추출 텍스트 검수 (수정 가능)")
                txt_input = st.text_area("에디터", value=st.session_state.extracted_text, height=500, label_visibility="collapsed")
                st.session_state.extracted_text = txt_input
                
                if st.button("🚀 분석 실행 (파일)", type="primary", use_container_width=True):
                    with st.spinner("AI 분석 중..."):
                        s_data = get_sheet_data_fresh(st.session_state.mode_key)[1]
                        send_img = st.session_state.file_bytes if len(txt_input) < 50 else None
                        res = get_analysis_hybrid(txt_input, send_img, s_data, st.session_state.mode_key)
                        
                        proc = []
                        temp_dict = {}
                        om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                        pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                        
                        for r in res:
                            key = (r.get('root_word',''), r.get('origin','혼'), r.get('pos','명사'))
                            if key not in temp_dict: temp_dict[key] = []
                            temp_dict[key].append(r.get('original_word',''))
                            
                        for (root, origin, pos), origs in temp_dict.items():
                            cnts = Counter(origs)
                            fmt_orig = ", ".join([f"{w}({c})" for w, c in cnts.items()])
                            proc.append({
                                "delete_check": False,
                                "count": f"{sum(cnts.values())}회",
                                "original_word": fmt_orig,
                                "root_word": root,
                                "origin": om.get(origin, origin),
                                "pos": pm.get(pos, pos)
                            })
                        
                        st.session_state.analysis_result = proc
                        st.session_state.step = 3
                        st.rerun()

    with tab2:
        direct_txt = st.text_area("분석할 텍스트를 입력하세요", height=400)
        if st.button("🚀 분석 실행 (Direct)", type="primary"):
            st.session_state.extracted_text = direct_txt
            with st.spinner("AI 분석 중..."):
                s_data = get_sheet_data_fresh(st.session_state.mode_key)[1]
                res = get_analysis_hybrid(direct_txt, None, s_data, st.session_state.mode_key)
                
                proc = []
                temp_dict = {}
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                for r in res:
                    key = (r.get('root_word',''), r.get('origin','혼'), r.get('pos','명사'))
                    if key not in temp_dict: temp_dict[key] = []
                    temp_dict[key].append(r.get('original_word',''))
                for (root, origin, pos), origs in temp_dict.items():
                    cnts = Counter(origs)
                    fmt_orig = ", ".join([f"{w}({c})" for w, c in cnts.items()])
                    proc.append({
                        "delete_check": False,
                        "count": f"{sum(cnts.values())}회",
                        "original_word": fmt_orig,
                        "root_word": root,
                        "origin": om.get(origin, origin),
                        "pos": pm.get(pos, pos)
                    })
                st.session_state.analysis_result = proc
                st.session_state.step = 3
                st.rerun()

# ---------------------------------------------------------
# STEP 3: 결과 확인 및 수정
# ---------------------------------------------------------
elif st.session_state.step == 3:
    st.header("📊 분석 결과 확인")
    
    @st.experimental_dialog("➕ 단어 수동 추가")
    def add_manual_item():
        with st.form("add_form"):
            o = st.text_input("원본 단어")
            r = st.text_input("원형")
            org = st.selectbox("분류", ["고","한","외","혼"])
            p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사","고유명사"])
            cnt = st.number_input("횟수", 1, 100, 1)
            if st.form_submit_button("추가"):
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                new_row = {
                    "delete_check": False,
                    "count": f"{cnt}회",
                    "original_word": f"{o}(수동)",
                    "root_word": r,
                    "origin": om.get(org, org),
                    "pos": pm.get(p, p)
                }
                st.session_state.analysis_result.append(new_row)
                sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
                send_data_with_retry(sheet, [datetime.now().isoformat(), o, r, org, p, 'add', '수동'])
                st.rerun()

    df_res = pd.DataFrame(st.session_state.analysis_result)
    edited = st.data_editor(
        df_res,
        column_config={
            "delete_check": st.column_config.CheckboxColumn("삭제"),
            "original_word": st.column_config.TextColumn("원본 (수정불가)", disabled=True),
            "root_word": st.column_config.TextColumn("원형"),
            "count": st.column_config.TextColumn("횟수"),
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사", "고유명사"])
        },
        use_container_width=True,
        num_rows="dynamic",
        key="editor"
    )
    
    if not edited.equals(df_res):
        st.session_state.analysis_result = edited.to_dict('records')

    ac1, ac2, ac3 = st.columns([1, 1, 2])
    with ac1:
        if st.button("➕ 단어 추가", use_container_width=True): add_manual_item()
    with ac2:
        if st.button("⛔ 선택 삭제", use_container_width=True):
            to_delete = edited[edited['delete_check']==True]
            if not to_delete.empty:
                sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
                logs = [[datetime.now().isoformat(), r['original_word'], r['root_word'], "", "", 'delete', 'User'] for _, r in to_delete.iterrows()]
                send_data_with_retry(sheet, logs, True)
                st.session_state.analysis_result = edited[edited['delete_check']==False].to_dict('records')
                st.rerun()
            else: st.toast("삭제할 항목을 선택해주세요.")

    def save_logic_common():
        sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
        logs = []
        for _, row in edited.iterrows():
            if not row['delete_check']:
                logs.append([datetime.now().isoformat(), row['original_word'], row['root_word'], clean_val_for_save(row['origin']), clean_val_for_save(row['pos']), 'modify', 'result'])
        send_data_with_retry(sheet, logs, True)
        
        valid = edited[edited['delete_check']==False].copy()
        valid['n_cnt'] = valid['count'].apply(lambda x: int(re.sub(r'[^0-9]', '', str(x))) if re.search(r'\d', str(x)) else 1)
        agg = valid.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({'n_cnt': 'sum'})
        
        p_num = str(st.session_state.page_idx + st.session_state.start_offset)
        temp_rows = []
        for _, item in agg.iterrows():
            val = f"{p_num}_{item['n_cnt']}" if item['n_cnt'] > 1 else p_num
            temp_rows.append({'구분': clean_val_for_save(item['origin']), '자료': item['root_word'], '쪽수1': val})
            
        st.session_state.master_df = merge_master_data(st.session_state.master_df, pd.DataFrame(temp_rows))
        save_backup_to_cloud(st.session_state.mode_key, st.session_state.master_df)

    with ac3:
        is_pdf = st.session_state.file_type and "pdf" in st.session_state.file_type
        total_p = 1
        if is_pdf and FITZ_AVAILABLE:
            try: total_p = len(fitz.open(stream=st.session_state.file_bytes, filetype="pdf"))
            except: pass
            
        if is_pdf and st.session_state.page_idx < total_p - 1:
            if st.button("💾 저장하고 다음 장 (▶)", type="primary", use_container_width=True):
                save_logic_common()
                st.session_state.page_idx += 1
                st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                st.session_state.analysis_result = []
                st.session_state.step = 2
                st.toast(f"저장되었습니다! {st.session_state.page_idx+1}쪽으로 이동합니다.")
                st.rerun()
        else:
            if st.button("💾 저장하기 (완료)", type="primary", use_container_width=True):
                save_logic_common()
                st.session_state.step = 4
                st.rerun()

# ---------------------------------------------------------
# STEP 4: 완료 및 다운로드
# ---------------------------------------------------------
elif st.session_state.step == 4:
    st.balloons()
    st.header("✅ 모든 작업이 완료되었습니다!")
    
    if st.session_state.master_df is not None:
        fname = "KR 국어 정리.xlsx" if st.session_state.mode_key == "SOUTH" else "KP 국어 정리.xlsx"
        
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as w: 
            st.session_state.master_df.to_excel(w, index=False)
        
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                label=f"📥 {fname} 다운로드",
                data=buf.getvalue(),
                file_name=fname,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary"
            )
        with c2:
            if st.button("🔄 처음으로 돌아가기 (새 작업)", use_container_width=True):
                st.session_state.clear()
                st.rerun()