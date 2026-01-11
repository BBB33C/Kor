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
import traceback

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
if 'last_raw_response' not in st.session_state: st.session_state.last_raw_response = "" # 디버깅 데이터 저장용

# =========================================================
# [1] 디자인: CSS 매직
# =========================================================
if st.session_state.step == 0:
    st.markdown("""
        <style>
            .stApp { background-color: #0e1117 !important; color: #ffffff !important; }
            .stTextArea textarea { font-family: 'Malgun Gothic', sans-serif !important; }
            div.block-container div[data-testid="column"] div.stButton > button {
                width: 100%; height: 320px;
                background-color: #262730 !important;
                border: 2px solid rgba(255,255,255,0.05) !important;
                border-radius: 20px !important;
                color: #eeeeee !important;
                font-size: 1.5rem !important; font-weight: 700 !important;
                transition: all 0.3s ease !important;
                box-shadow: 0 4px 10px rgba(0,0,0,0.3) !important;
            }
            div.block-container div[data-testid="column"] div.stButton > button:hover {
                transform: translateY(-8px);
                background-color: #2b2c36 !important;
            }
        </style>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
        <style>
            .stApp { background-color: #0e1117 !important; color: #ffffff !important; }
            .stTextArea textarea { font-family: 'Malgun Gothic', sans-serif !important; font-size: 16px !important; line-height: 1.6 !important; }
            .stButton button { border-radius: 8px; font-weight: bold; height: auto; }
        </style>
    """, unsafe_allow_html=True)

# 라이브러리 로드
try:
    import pdfplumber
    PLUMBER_AVAILABLE = True
except ImportError:
    PLUMBER_AVAILABLE = False
try:
    import fitz
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
        if res.status_code == 200:
            return res.json()['candidates'][0]['content']['parts'][0]['text']
        else:
            return f"API Error: {res.status_code}"
    except Exception as e:
        return f"Request Error: {str(e)}"

def api_call_vision_ocr(image_bytes):
    return api_call_direct("이 이미지의 텍스트를 보이는 그대로 추출해줘. 줄바꿈 유지.", image_bytes) or ""

def extract_text_unified(file_bytes, file_type, page_idx):
    raw_text = ""
    if "image" in file_type: raw_text = api_call_vision_ocr(file_bytes)
    elif "pdf" in file_type:
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    if page_idx < len(pdf.pages): raw_text = pdf.pages[page_idx].extract_text()
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

# [수정] AI 분석 함수 (Raw Response 반환 추가)
def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    prompt = f"""
    당신은 '{"대한민국 표준어" if mode_key=="SOUTH" else "북한 문화어"}' 형태소 분석 전문가입니다.
    {generate_prompt_from_sheet(sheet_data)}
    
    [분석 규칙]
    1. 조사/어미 제거, '하다' 용언은 명사로 분류.
    2. 품사: 명사, 동사, 형용사, 부사, 관형사, 대명사, 고유명사.
    3. **동음이의어: 원형 뒤에 괄호로 뜻 구분 (필수).** (예: 배(과일), 배(선박))
    4. **인명/지명: 원형 뒤에 (이름)/(지명) 표기 (필수).** (예: 지혜(이름), 서울(지명))
    5. 출력: JSON 포맷 (반드시 빈 리스트 []가 아닌 유효한 데이터를 포함할 것)
    """
    
    # 1. API 호출
    raw_response = api_call_direct(prompt + f"\n[텍스트]:\n{text[:5000]}", image_bytes)
    
    if not raw_response:
        return [], "No response from API"

    # 2. JSON 파싱 시도
    try:
        clean_json = raw_response.replace("```json", "").replace("```", "").strip()
        match = re.search(r'\[.*\]', clean_json, re.DOTALL)
        if match:
            return json.loads(match.group()), raw_response
        else:
            return [], raw_response
    except Exception as e:
        return [], f"JSON Parsing Error: {str(e)}\nRaw Response: {raw_response}"

# =========================================================
# [4] UI: 메인 루프
# =========================================================

# 사이드바 (관리자 모드 제어)
with st.sidebar:
    st.markdown("### ⚙️ 설정")
    if st.session_state.debug_mode:
        st.success("🐞 디버깅 모드 ON")
        if st.button("디버깅 모드 끄기", use_container_width=True):
            st.session_state.debug_mode = False
            st.rerun()
    else:
        st.markdown("<br>"*5, unsafe_allow_html=True)
        if st.button("🛠️ 관리자 모드 켜기", use_container_width=True):
            st.session_state.debug_mode = True
            st.rerun()

# ---------------------------------------------------------
# STEP 0: 시작 화면
# ---------------------------------------------------------
if st.session_state.step == 0:
    st.markdown("<h1 style='text-align: center; margin-bottom: 20px;'>📚 국어활동 AI 분석기</h1>", unsafe_allow_html=True)
    if st.session_state.debug_mode:
        st.warning("🚧 [관리자 모드] 활성화됨. 분석 시 상세 로그가 표시됩니다.")
    else:
        st.markdown("<p style='text-align: center; color: #888; margin-bottom: 50px;'>원하는 언어 규범을 선택하여 분석을 시작하세요.</p>", unsafe_allow_html=True)
    
    _, c_south, c_north, _ = st.columns([1, 4, 4, 1])
    
    with c_south:
        st.markdown("""
        <style>
            div[data-testid="column"]:nth-of-type(2) div.stButton > button { border-color: rgba(41, 121, 255, 0.4) !important; box-shadow: 0 0 10px rgba(41, 121, 255, 0.1) !important; }
            div[data-testid="column"]:nth-of-type(2) div.stButton > button:hover { border-color: #2979ff !important; box-shadow: 0 0 35px rgba(41, 121, 255, 0.7) !important; color: #2979ff !important; }
        </style>
        """, unsafe_allow_html=True)
        if st.button("🏛️\n\n대한민국 표준어\n\n(표준국어대사전 기준)\n\n[ 시작하기 ]", key="btn_south", use_container_width=True):
            st.session_state.mode_key = "SOUTH"
            st.session_state.step = 1
            st.toast("✅ 대한민국 학습 서버에 연결되었습니다.")
            time.sleep(0.5); st.rerun()

    with c_north:
        st.markdown("""
        <style>
            div[data-testid="column"]:nth-of-type(3) div.stButton > button { border-color: rgba(255, 23, 68, 0.4) !important; box-shadow: 0 0 10px rgba(255, 23, 68, 0.1) !important; }
            div[data-testid="column"]:nth-of-type(3) div.stButton > button:hover { border-color: #ff1744 !important; box-shadow: 0 0 35px rgba(255, 23, 68, 0.7) !important; color: #ff1744 !important; }
        </style>
        """, unsafe_allow_html=True)
        if st.button("🏔️\n\n북한 문화어\n\n(문화어 규범 기준)\n\n[ 시작하기 ]", key="btn_north", use_container_width=True):
            st.session_state.mode_key = "NORTH"
            st.session_state.step = 1
            st.toast("✅ 북한 학습 서버에 연결되었습니다.")
            time.sleep(0.5); st.rerun()

# ---------------------------------------------------------
# STEP 1: 데이터 소스 선택
# ---------------------------------------------------------
elif st.session_state.step == 1:
    st.header("📂 데이터 소스 선택")
    if st.session_state.debug_mode: st.info(f"🔧 현재 모드: {st.session_state.mode_key}")

    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.subheader("📂 이어하기")
            up_excel = st.file_uploader("엑셀 파일 업로드", type=['xlsx'])
            if up_excel:
                try:
                    st.session_state.master_df = pd.read_excel(up_excel)
                    st.session_state.step = 2
                    st.rerun()
                except Exception as e: st.error("파일 오류")
    with col2:
        with st.container(border=True):
            st.subheader("🆕 새로 시작하기")
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
    c_title, c_home = st.columns([8, 2])
    with c_title: st.header("📝 분석 자료 입력")
    with c_home:
        if st.button("🏠 처음으로\n(초기화)", use_container_width=True):
            st.session_state.clear(); st.rerun()

    tab1, tab2 = st.tabs(["📄 파일 분석", "✍️ 직접 입력"])
    
    # [수정] 3. 실행 로직 함수 (변수 인자 명확화 및 빈 데이터 처리)
    def run_analysis_logic(txt, img=None):
        if not txt or not txt.strip():
            st.warning("⚠️ 분석할 텍스트가 없습니다. 내용을 확인해주세요.")
            return

        with st.spinner("AI 분석 중... (잠시만 기다려주세요)"):
            s_data = get_sheet_data_fresh(st.session_state.mode_key)[1]
            res, raw_response = get_analysis_hybrid(txt, img, s_data, st.session_state.mode_key)
            
            # [디버깅 저장]
            st.session_state.last_raw_response = raw_response
            
            proc = []
            temp_dict = {}
            om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
            pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
            
            for r in res:
                # [버그 수정] 빈 데이터(Empty String/Null) 필터링
                o_word = str(r.get('original_word', '')).strip()
                r_word = str(r.get('root_word', '')).strip()
                
                # 빈 문자열이면 즉시 스킵
                if not o_word or o_word.lower() == 'none': continue
                if not r_word or r_word.lower() == 'none': continue

                key = (r_word, r.get('origin','혼'), r.get('pos','명사'))
                if key not in temp_dict: temp_dict[key] = []
                temp_dict[key].append(o_word)
            
            for (root, origin, pos), origs in temp_dict.items():
                cnts = Counter(origs)
                # 포맷팅 시 빈 문자열 제외
                valid_items = [f"{w}({c})" for w, c in cnts.items() if w and w.strip()]
                if not valid_items: continue
                
                fmt_orig = ", ".join(valid_items)
                
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

    with tab1:
        file = st.file_uploader("파일 업로드", type=['pdf', 'png', 'jpg'])
        if file:
            file_bytes = file.getvalue()
            if st.session_state.file_bytes != file_bytes:
                st.session_state.file_bytes = file_bytes
                st.session_state.file_type = file.type
                st.session_state.page_idx = 0
                st.session_state.extracted_text = extract_text_unified(file_bytes, file.type, 0)
            
            c1, c2 = st.columns(2)
            with c1:
                st.caption("미리보기")
                img = get_page_image(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                if img: st.image(img, use_container_width=True)
                if "pdf" in st.session_state.file_type:
                    if st.button("▶ 다음 페이지"):
                        st.session_state.page_idx += 1
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                        st.rerun()
                    st.session_state.start_offset = st.number_input("시작 쪽수", value=st.session_state.start_offset)
            with c2:
                txt_input = st.text_area("에디터", value=st.session_state.extracted_text, height=500)
                st.session_state.extracted_text = txt_input
                # [수정] 파일 탭 전용 버튼 (txt_input 전달)
                if st.button("🚀 분석 실행", type="primary", use_container_width=True, key="run_file"):
                    run_analysis_logic(txt_input, st.session_state.file_bytes)

    with tab2:
        direct_txt = st.text_area("텍스트 입력", height=400)
        # [수정] 직접 입력 탭 전용 버튼 (direct_txt 전달)
        if st.button("🚀 분석 실행", type="primary", key="run_direct"):
            st.session_state.extracted_text = direct_txt
            run_analysis_logic(direct_txt)

# ---------------------------------------------------------
# STEP 3: 결과 확인 (디버그 & 오류 해결)
# ---------------------------------------------------------
elif st.session_state.step == 3:
    st.header("📊 분석 결과 확인")
    
    # [수정] 디버그 패널 (Raw Response 확인)
    if st.session_state.debug_mode:
        with st.expander("🔴 [DEBUG] AI 응답 원본 확인", expanded=True):
            st.info("AI가 반환한 Raw Data입니다. 오류 발생 시 확인하세요.")
            st.code(st.session_state.get('last_raw_response', '데이터 없음'))

    with st.expander("📝 원문 텍스트 보기 (클릭)", expanded=False):
        st.text_area("분석 대상", value=st.session_state.extracted_text, height=200, disabled=True)

    # Dialog 함수 호환성 처리
    if hasattr(st, "dialog"): dlg = st.dialog
    else: dlg = st.experimental_dialog

    @dlg("➕ 단어 추가")
    def add_manual_item():
        with st.form("add_form"):
            o = st.text_input("원본")
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
            "original_word": st.column_config.TextColumn("원본", disabled=True),
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
            else: st.toast("선택된 항목이 없습니다.")

    with ac3:
        if st.button("💾 저장하기 (완료)", type="primary", use_container_width=True):
            save_logic_common()
            st.session_state.step = 4
            st.rerun()

# ---------------------------------------------------------
# STEP 4: 완료
# ---------------------------------------------------------
elif st.session_state.step == 4:
    st.balloons()
    st.header("✅ 완료되었습니다!")
    
    if st.session_state.master_df is not None:
        fname = "KR_Result.xlsx" if st.session_state.mode_key == "SOUTH" else "KP_Result.xlsx"
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as w: 
            st.session_state.master_df.to_excel(w, index=False)
        
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("📥 다운로드", buf.getvalue(), fname, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, type="primary")
        with c2:
            if st.button("🔄 처음으로", use_container_width=True):
                st.session_state.clear()
                st.rerun()