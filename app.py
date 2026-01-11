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

# 세션 상태 초기화 (모든 작업 상태 보존)
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
if 'last_raw_response' not in st.session_state: st.session_state.last_raw_response = ""
if 'current_tab_idx' not in st.session_state: st.session_state.current_tab_idx = 0 
if 'is_finished' not in st.session_state: st.session_state.is_finished = False

# =========================================================
# [1] 디자인: CSS 매직 (고급 색상 및 중앙 정렬 유지)
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
            div.stRadio > div[role="radiogroup"] { display: flex; flex-direction: row; gap: 10px; }
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
# [2] 구글 시트 및 API 유틸리티
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
# [3] 데이터 병합 및 세부 로직 (생략 없이 복구)
# =========================================================
def clean_val_for_save(v):
    if isinstance(v, str): 
        chars_to_remove = ['🔵 ', '🟢 ', '🔴 ', '🟣 ', '📦 ', '🏃 ', '🎨 ', '⚡ ', '🔍 ', '👤 ', '❗ ']
        for char in chars_to_remove:
            v = v.replace(char, '')
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
        # 이전 쪽수 수집
        for c in page_cols_old:
            val = row.get(f"{c}_old", row.get(c))
            if pd.notna(val) and str(val).strip() not in ['nan', '', 'None']: pages.append(str(val))
        # 새 쪽수 수집
        for c in page_cols_new:
            val = row.get(f"{c}_new", row.get(c))
            if pd.notna(val) and str(val).strip() not in ['nan', '', 'None']: pages.append(str(val))
            
        unique_pages = sorted(list(set(pages)))
        for i, p in enumerate(unique_pages): 
            new_row[f"쪽수{i+1}"] = p
        final_rows.append(new_row)
        
    result_df = pd.DataFrame(final_rows)
    result_df['출연횟수'] = result_df.apply(calc_freq, axis=1)
    
    # 정렬 규칙 (고-한-외-혼 순)
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    result_df['sk'] = result_df['구분'].map(sort_map).fillna(5)
    result_df = result_df.sort_values(['sk', '자료']).drop('sk', axis=1)
    return result_df

def save_logic_common():
    sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
    logs = []
    # 현재 에디터 상태를 세션에 반영
    res_df = pd.DataFrame(st.session_state.analysis_result)
    for _, row in res_df.iterrows():
        if not row.get('삭제', False):
            logs.append([
                datetime.now().isoformat(), 
                row.get('원본',''), 
                row.get('원형',''), 
                clean_val_for_save(row.get('분류','')), 
                clean_val_for_save(row.get('품사','')), 
                'modify', 
                'result'
            ])
    if logs:
        send_data_with_retry(sheet, logs, True)
    
    # 마스터 엑셀 데이터 업데이트
    valid = res_df[res_df['삭제']==False].copy()
    valid['n_cnt'] = valid['횟수'].apply(lambda x: int(re.sub(r'[^0-9]', '', str(x))) if re.search(r'\d', str(x)) else 1)
    agg = valid.groupby(['원형', '분류', '품사'], as_index=False).agg({'n_cnt': 'sum'})
    
    p_num = str(st.session_state.page_idx + st.session_state.start_offset)
    temp_rows = []
    for _, item in agg.iterrows():
        val = f"{p_num}_{item['n_cnt']}" if item['n_cnt'] > 1 else p_num
        temp_rows.append({'구분': clean_val_for_save(item['분류']), '자료': item['원형'], '쪽수1': val})
        
    st.session_state.master_df = merge_master_data(st.session_state.master_df, pd.DataFrame(temp_rows))
    save_backup_to_cloud(st.session_state.mode_key, st.session_state.master_df)

# =========================================================
# [4] AI 분석 엔진 및 추출 로직
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    for row in sheet_data[-100:]:
        orig = row.get('original_word', '')
        root = row.get('root_word', '')
        origin = row.get('origin', '')
        pos = row.get('pos', '')
        if row.get('action') == 'delete':
            rules.append(f"- [제외 권장]: '{orig}'는 분석 결과에서 제외하세요.")
        elif row.get('action') in ['add', 'modify']:
            rules.append(f"- [고정 규칙]: '{orig}'는 원형:'{root}', 분류:'{origin}', 품사:'{pos}'로 고정하세요.")
    return "\n[학습된 사용자 규칙]:\n" + "\n".join(rules) + "\n" if rules else ""

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

def extract_text_unified(file_bytes, file_type, page_idx):
    raw_text = ""
    if "image" in file_type: raw_text = api_call_direct("이 이미지의 텍스트를 보이는 그대로 추출해줘.", file_bytes) or ""
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
                    raw_text = api_call_direct("이 이미지의 텍스트를 추출해줘.", pix.tobytes("png")) or ""
             except: pass
    lines = raw_text.split('\n')
    cleaned = [l for l in lines if not re.search(r'\.indd|\d{4}-\d{2}-\d{2}|오후|오전', l)]
    return "\n".join(cleaned).strip()

def get_page_image(file_bytes, file_type, page_idx):
    if "image" in file_type: return file_bytes
    if "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            if page_idx < len(doc):
                pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                return pix.tobytes("png")
        except: pass
    return None

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    # '명사+하다' 강제 규칙 삭제됨
    prompt = f"""
    당신은 국어 형태소 분석 전문가입니다. 아래 규칙에 따라 텍스트를 분석하세요.
    {generate_prompt_from_sheet(sheet_data)}
    
    [분석 및 제외 규칙]
    1. **특수문자 및 문장부호 제거**: 마침표, 쉼표, 물음표, 느낌표, 괄호 등 모든 특수문자는 절대 결과에 포함하지 마세요.
    2. **조사 및 어미 제외**: '은/는/이/가' 등의 조사와 '-다/요' 등의 어미는 독립된 항목으로 출력하지 마세요.
    3. **의존 명사 포함**: '것', '수', '만큼' 등의 의존 명사는 '명사'로 분류하여 포함하세요.
    
    [어종(Origin) 분류 기준]
    1. **한 (한자어)**: 한자 기반 단어. (예: 학교, 분석, 질문, 감탄)
    2. **고 (고유어)**: 순우리말. (예: 하늘, 바다, 가다, 예쁘다)
    3. **외 (외래어)**: 서양 언어 유래 단어. (예: 버스, 컴퓨터, 데이터)
    
    [출력 포맷]
    반드시 아래 **한글 키**를 가진 JSON 리스트로 출력하세요.
    - 원본: 문장에서의 단어
    - 원형: 기본형
    - 분류: 어종 (고/한/외/혼)
    - 품사: 품사 (명사/동사/형용사/부사/관형사/대명사/감탄사)
    """
    raw_response = api_call_direct(prompt + f"\n[분석할 텍스트]:\n{text[:5000]}", image_bytes)
    if not raw_response: return [], "No response from API"
    try:
        clean_json = raw_response.replace("```json", "").replace("```", "").strip()
        match = re.search(r'\[.*\]', clean_json, re.DOTALL)
        if match: return json.loads(match.group()), raw_response
        else: return [], raw_response
    except Exception as e: return [], f"JSON Error: {str(e)}\nRaw: {raw_response}"

# =========================================================
# [5] UI: 메인 루프 (Wizard)
# =========================================================

# 사이드바 설정
with st.sidebar:
    st.markdown("### ⚙️ 설정")
    if st.session_state.debug_mode:
        st.success("🐞 디버깅 모드 ON")
        if st.button("디버깅 모드 끄기", use_container_width=True):
            st.session_state.debug_mode = False; st.rerun()
    else:
        st.markdown("<br>"*5, unsafe_allow_html=True)
        if st.button("🛠️ 관리자 모드 켜기", use_container_width=True):
            st.session_state.debug_mode = True; st.rerun()

# STEP 0: 시작 화면
if st.session_state.step == 0:
    st.markdown("<h1 style='text-align: center; margin-bottom: 20px;'>📚 국어활동 AI 분석기</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #888; margin-bottom: 50px;'>분석할 언어 규범을 선택하세요.</p>", unsafe_allow_html=True)
    _, c_south, c_north, _ = st.columns([1, 4, 4, 1])
    
    with c_south:
        st.markdown("<style>div[data-testid='column']:nth-of-type(2) div.stButton > button { border-color: rgba(41, 121, 255, 0.4) !important; box-shadow: 0 0 10px rgba(41, 121, 255, 0.1) !important; } div[data-testid='column']:nth-of-type(2) div.stButton > button:hover { border-color: #2979ff !important; box-shadow: 0 0 35px rgba(41, 121, 255, 0.7) !important; color: #2979ff !important; }</style>", unsafe_allow_html=True)
        if st.button("🏛️\n\n대한민국 표준어\n\n(표준국어대사전 기준)\n\n[ 시작하기 ]", key="btn_south", use_container_width=True):
            st.session_state.mode_key = "SOUTH"; st.session_state.step = 1; st.rerun()

    with c_north:
        st.markdown("<style>div[data-testid='column']:nth-of-type(3) div.stButton > button { border-color: rgba(255, 23, 68, 0.4) !important; box-shadow: 0 0 10px rgba(255, 23, 68, 0.1) !important; } div[data-testid='column']:nth-of-type(3) div.stButton > button:hover { border-color: #ff1744 !important; box-shadow: 0 0 35px rgba(255, 23, 68, 0.7) !important; color: #ff1744 !important; }</style>", unsafe_allow_html=True)
        if st.button("🏔️\n\n북한 문화어\n\n(문화어 규범 기준)\n\n[ 시작하기 ]", key="btn_north", use_container_width=True):
            st.session_state.mode_key = "NORTH"; st.session_state.step = 1; st.rerun()

# STEP 1: 데이터 소스 선택
elif st.session_state.step == 1:
    st.header("📂 데이터 소스 선택")
    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.subheader("📂 이어하기")
            up_excel = st.file_uploader("기존 분석 엑셀 업로드", type=['xlsx'])
            if up_excel:
                try:
                    st.session_state.master_df = pd.read_excel(up_excel)
                    st.session_state.step = 2; st.rerun()
                except: st.error("엑셀 파일 오류")
    with col2:
        with st.container(border=True):
            st.subheader("🆕 새로 시작하기")
            if st.button("새 프로젝트 생성", use_container_width=True):
                st.session_state.master_df = None; st.session_state.step = 2; st.rerun()
    if st.button("⬅️ 모드 다시 선택"): st.session_state.step = 0; st.rerun()

# STEP 2: 자료 입력 (상태 유지 기능 강화)
elif st.session_state.step == 2:
    st.session_state.is_finished = False # 완료 상태 초기화
    c_title, c_home = st.columns([8, 2])
    with c_title: st.header("📝 분석 자료 입력")
    with c_home:
        if st.button("🏠 처음으로\n(초기화)", use_container_width=True):
            st.session_state.clear(); st.rerun()

    input_method = st.radio("입력 방식 선택", ["📄 파일 분석", "✍️ 직접 입력"], horizontal=True, index=st.session_state.current_tab_idx, label_visibility="collapsed")

    def run_analysis_action(txt, img=None):
        if not txt or not txt.strip(): st.warning("⚠️ 분석할 텍스트가 없습니다."); return
        with st.spinner("AI 분석 중... (어종 분류 정밀 분석 중)"):
            s_data = get_sheet_data_fresh(st.session_state.mode_key)[1]
            res, raw = get_analysis_hybrid(txt, img, s_data, st.session_state.mode_key)
            st.session_state.last_raw_response = raw
            
            proc = []; temp_dict = {}
            om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
            pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사', '감탄사':'❗ 감탄사'}
            
            for r in res:
                o_word = str(r.get('원본') or r.get('original_word') or '').strip()
                r_word = str(r.get('원형') or r.get('root_word') or '').strip()
                origin_v = str(r.get('분류') or r.get('origin') or '혼').strip()
                pos_v = str(r.get('품사') or r.get('pos') or '명사').strip()
                
                if not o_word or not r_word or not re.search(r'[가-힣a-zA-Z0-9]', o_word): continue
                if pos_v in ['조사', '어미', '문장부호', '특수문자']: continue

                key = (r_word, origin_v, pos_v)
                if key not in temp_dict: temp_dict[key] = []
                temp_dict[key].append(o_word)
                
            for (root, origin, pos), origs in temp_dict.items():
                cnts = Counter(origs)
                proc.append({
                    "삭제": False, "횟수": f"{sum(cnts.values())}회", 
                    "원본": ", ".join([f"{w}({c})" for w, c in cnts.items() if w]), 
                    "원형": root, "분류": om.get(origin, origin), "품사": pm.get(pos, pos)
                })
            
            st.session_state.analysis_result = proc
            st.session_state.step = 3; st.rerun()

    if input_method == "📄 파일 분석":
        st.session_state.current_tab_idx = 0
        file = st.file_uploader("파일 업로드", type=['pdf', 'png', 'jpg'])
        if file:
            fb = file.getvalue()
            if st.session_state.file_bytes != fb:
                st.session_state.file_bytes = fb; st.session_state.file_type = file.type
                st.session_state.page_idx = 0; st.session_state.extracted_text = extract_text_unified(fb, file.type, 0)
            
            c1, c2 = st.columns(2)
            with c1:
                st.caption("미리보기")
                img = get_page_image(fb, file.type, st.session_state.page_idx)
                if img: st.image(img, use_container_width=True)
                if "pdf" in file.type:
                    if st.button("▶ 다음 페이지"):
                        st.session_state.page_idx += 1
                        st.session_state.extracted_text = extract_text_unified(fb, file.type, st.session_state.page_idx); st.rerun()
                    st.session_state.start_offset = st.number_input("시작 쪽수", value=st.session_state.start_offset)
            with c2:
                txt_in = st.text_area("에디터", value=st.session_state.extracted_text, height=500)
                st.session_state.extracted_text = txt_in
                if st.button("🚀 분석 실행", type="primary", use_container_width=True, key="run_file"):
                    run_analysis_action(txt_in, fb)
    else:
        st.session_state.current_tab_idx = 1
        direct_t = st.text_area("텍스트 입력 창", value=st.session_state.extracted_text, height=450)
        st.session_state.extracted_text = direct_t
        if st.button("🚀 분석 실행", type="primary", use_container_width=True, key="run_direct"):
            run_analysis_action(direct_t)

# STEP 3: 결과 확인 (페이지 전환 없이 다운로드 섹션 포함)
elif st.session_state.step == 3:
    c_head, c_btn = st.columns([8, 2])
    with c_head: st.header("📊 분석 결과 확인")
    with c_btn:
        if st.button("⬅️ 입력 수정하기", use_container_width=True):
            st.session_state.step = 2; st.rerun()
            
    if st.session_state.debug_mode:
        with st.expander("🔴 [DEBUG] AI Raw Data"): st.code(st.session_state.last_raw_response)
    with st.expander("📝 분석 대상 원문 확인"):
        st.text_area("원문", value=st.session_state.extracted_text, height=200, disabled=True)

    dlg_func = st.dialog if hasattr(st, "dialog") else st.experimental_dialog
    @dlg_func("➕ 단어 추가")
    def add_manual():
        with st.form("manual_add"):
            o = st.text_input("원본 단어"); r = st.text_input("원형")
            org = st.selectbox("분류", ["고","한","외","혼"])
            p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사","고유명사","감탄사"])
            cnt = st.number_input("횟수", 1, 100, 1)
            if st.form_submit_button("추가 완료"):
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사', '감탄사':'❗ 감탄사'}
                st.session_state.analysis_result.append({
                    "삭제": False, "횟수": f"{cnt}회", "원본": f"{o}(수동)", "원형": r, 
                    "분류": om.get(org, org), "품사": pm.get(p, p)
                })
                # 시트 로그 전송
                sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
                send_data_with_retry(sheet, [datetime.now().isoformat(), o, r, org, p, 'add', '수동'])
                st.rerun()

    # 데이터 에디터 출력
    df_res = pd.DataFrame(st.session_state.analysis_result)
    edited = st.data_editor(
        df_res,
        column_config={
            "삭제": st.column_config.CheckboxColumn("삭제"),
            "원본": st.column_config.TextColumn("원본", disabled=True),
            "원형": st.column_config.TextColumn("원형"),
            "횟수": st.column_config.TextColumn("횟수"),
            "분류": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "품사": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사", "고유명사", "❗ 감탄사"])
        },
        use_container_width=True, num_rows="dynamic", key="editor_grid"
    )
    if not edited.equals(df_res): st.session_state.analysis_result = edited.to_dict('records')

    # 하단 인터랙션 영역
    if not st.session_state.is_finished:
        b1, b2, b3 = st.columns([1, 1, 2])
        with b1:
            if st.button("➕ 단어 추가", use_container_width=True): add_manual()
        with b2:
            if st.button("⛔ 선택 삭제", use_container_width=True):
                st.session_state.analysis_result = edited[edited['삭제']==False].to_dict('records'); st.rerun()
        with b3:
            if st.button("💾 저장하기 (완료)", type="primary", use_container_width=True):
                save_logic_common()
                st.session_state.is_finished = True; st.balloons(); st.rerun()
    else:
        # [완료 후 섹션] 페이지 전환 없이 즉시 노출
        st.success("✅ 모든 분석 데이터가 안전하게 저장되었습니다!")
        fname = f"국어활동_{st.session_state.mode_key}_{datetime.now().strftime('%m%d_%H%M')}.xlsx"
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
            if st.button("🔄 입력창으로 돌아가기 (연속 작업)", use_container_width=True):
                st.session_state.step = 2
                st.session_state.is_finished = False
                st.rerun()