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

# 세션 상태 초기화 (비교 학습을 위한 initial_draft 변수 추가)
if 'step' not in st.session_state: st.session_state.step = 0
if 'mode_key' not in st.session_state: st.session_state.mode_key = None
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'initial_draft' not in st.session_state: st.session_state.initial_draft = [] # AI 초안 저장용
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
# [1] 디자인: CSS 매직 (메인 대형 버튼 및 상세 스타일 보존)
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
                white-space: pre-wrap !important;
                line-height: 1.4 !important;
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
# [2] 구글 시트 연동 및 API 유틸리티
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
            ws.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context", "initial_root", "initial_origin"])
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
# [3] 데이터 가공 및 비교 학습 엔진 (핵심 로직)
# =========================================================
def clean_val_for_save(v):
    if isinstance(v, str): 
        for char in ['🔵 ', '🟢 ', '🔴 ', '🟣 ', '📦 ', '🏃 ', '🎨 ', '⚡ ', '🔍 ', '👤 ', '❗ ']:
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
        for c in page_cols_old:
            val = row.get(f"{c}_old", row.get(c))
            if pd.notna(val) and str(val).strip() not in ['nan', '', 'None']: pages.append(str(val))
        for c in page_cols_new:
            val = row.get(f"{c}_new", row.get(c))
            if pd.notna(val) and str(val).strip() not in ['nan', '', 'None']: pages.append(str(val))
            
        unique_pages = sorted(list(set(pages)))
        for i, p in enumerate(unique_pages): new_row[f"쪽수{i+1}"] = p
        final_rows.append(new_row)
        
    result_df = pd.DataFrame(final_rows)
    result_df['출연횟수'] = result_df.apply(calc_freq, axis=1)
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    result_df['sk'] = result_df['구분'].map(sort_map).fillna(5)
    result_df = result_df.sort_values(['sk', '자료']).drop('sk', axis=1)
    return result_df

# [핵심] 비교 학습 엔진 기반 저장 로직
def save_logic_with_learning():
    sheet, _ = get_sheet_data_fresh(st.session_state.mode_key)
    now = datetime.now().isoformat()
    learning_logs = []
    
    # 데이터 준비
    final_results = pd.DataFrame(st.session_state.analysis_result)
    initial_draft = pd.DataFrame(st.session_state.initial_draft)
    
    # 1. 교정 및 삭제 학습 (초안 기준으로 최종안 비교)
    for _, draft_row in initial_draft.iterrows():
        orig = draft_row['원본']
        # 최종안에서 같은 원본 단어를 찾음
        match = final_results[final_results['원본'] == orig]
        
        if match.empty or match.iloc[0]['삭제']:
            # 삭제 학습: 초안에는 있었으나 최종안에서 사라졌거나 삭제 체크된 경우
            learning_logs.append([now, orig, draft_row['원형'], draft_row['분류'], draft_row['품사'], 'delete', 'Engine-Compare', draft_row['원형'], draft_row['분류']])
        else:
            final_row = match.iloc[0]
            # 교정 학습: 원형, 분류, 품사 중 하나라도 바뀐 경우
            if (draft_row['원형'] != final_row['원형'] or 
                clean_val_for_save(draft_row['분류']) != clean_val_for_save(final_row['분류']) or 
                clean_val_for_save(draft_row['품사']) != clean_val_for_save(final_row['품사'])):
                
                learning_logs.append([
                    now, orig, final_row['원형'], 
                    clean_val_for_save(final_row['분류']), 
                    clean_val_for_save(final_row['품사']), 
                    'modify', 'Engine-Compare', 
                    draft_row['원형'], draft_row['분류']
                ])
                
    # 2. 추가 학습 (최종안에만 새로 생긴 단어)
    draft_originals = initial_draft['원본'].tolist()
    for _, final_row in final_results.iterrows():
        if final_row['원본'] not in draft_originals and not final_row['삭제']:
            learning_logs.append([
                now, final_row['원본'], final_row['원형'], 
                clean_val_for_save(final_row['분류']), 
                clean_val_for_save(final_row['품사']), 
                'add', 'Engine-New', '', ''
            ])
            
    # 학습 로그 전송
    if learning_logs:
        send_data_with_retry(sheet, learning_logs, True)
    
    # 마스터 엑셀 데이터 업데이트
    valid = final_results[final_results['삭제']==False].copy()
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
# [4] AI 분석 엔진 (프롬프트 강화)
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    # 최신 100개의 교정 이력을 바탕으로 프롬프트 생성
    for row in sheet_data[-150:]:
        orig, root, origin, pos, action = row.get('original_word',''), row.get('root_word',''), row.get('origin',''), row.get('pos',''), row.get('action','')
        if action == 'delete': rules.append(f"- '{orig}'는 불필요한 단어로 분류되니 분석에서 제외하십시오.")
        elif action == 'modify' or action == 'add':
            rules.append(f"- '{orig}'의 분석 결과는 원형:'{root}', 분류:'{origin}', 품사:'{pos}'가 되어야 합니다.")
    return "\n[과거 사용자 교정 데이터 (학습 내용)]:\n" + "\n".join(rules) + "\n"

def api_call_direct(prompt, image_bytes=None):
    if not API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    parts = [{"text": prompt}]
    if image_bytes: parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_bytes).decode('utf-8')}})
    try:
        res = requests.post(url, headers=headers, json={"contents": [{"parts": parts}]}, timeout=300)
        if res.status_code == 200: return res.json()['candidates'][0]['content']['parts'][0]['text']
        return f"API Error: {res.status_code}"
    except Exception as e: return f"Error: {str(e)}"

def extract_text_unified(file_bytes, file_type, page_idx):
    raw_text = ""
    if "image" in file_type: raw_text = api_call_direct("이 이미지 속의 텍스트를 모두 추출하세요. 줄바꿈을 유지하세요.", file_bytes) or ""
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
    return raw_text.strip()

def get_page_image(file_bytes, file_type, page_idx):
    if "image" in file_type: return file_bytes
    if "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            if page_idx < len(doc): return doc[page_idx].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
        except: pass
    return None

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    # 어종 분류 기준 및 동음이의어/인명/지명 로직 강화
    prompt = f"""
    당신은 대한민국 국어학자이자 형태소 분석 전문가입니다. 아래 지침에 따라 텍스트를 정밀 분석하여 JSON으로 출력하십시오.
    {generate_prompt_from_sheet(sheet_data)}
    
    [분석 핵심 원칙]
    1. **조사 및 어미 완전 제거**: '은/는/이/가/을/를' 등 조사와 '-다/요/네' 등 어미는 절대 결과 리스트에 포함하지 마십시오.
    2. **특수문자 배제**: 문장부호(., ?!)와 괄호, 기호 등은 분석 결과에서 제외하십시오.
    3. **의존 명사**: '것', '수', '만큼', '데', '바' 등은 '명사' 품사로 분류하여 결과에 포함하십시오.
    4. **동음이의어 정밀 구분**: 문맥상 뜻이 여러 가지인 단어는 원형 뒤에 괄호로 뜻을 구분하십시오 (예: 배(과일), 배(선박), 배(신체)).
    5. **개체명 인식 (인명/지명)**: 문맥상 사람의 성명은 원형 뒤에 '(이름)', 지역 명칭은 '(지명)'을 반드시 표기하십시오 (예: 지혜(이름), 서울(지명), 금강산(지명)).
    
    [어종(Origin) 판별 가이드]
    - **한 (한자어)**: 한자 근거 단어. 순우리말처럼 느껴져도 한자가 있다면 '한'입니다. (예: 학교, 질문, 지혜, 분석, 감사)
    - **고 (고유어)**: 순수 우리말. (예: 하늘, 바다, 가다, 예쁘다, 아리랑)
    - **외 (외래어)**: 서구권 등 외국 유래어. (예: 버스, 컴퓨터, 데이터)
    - **혼 (혼종어)**: 위 어종들이 섞인 경우.
    
    [출력 양식: 반드시 아래 한글 키를 가진 JSON 리스트]
    - [ {{"원본": "...", "원형": "...", "분류": "고/한/외/혼", "품사": "명사/동사/형용사/부사/관형사/대명사/감탄사"}} ]
    """
    raw = api_call_direct(prompt + f"\n[분석 대상 텍스트]:\n{text[:5000]}", image_bytes)
    if not raw: return [], "No response"
    try:
        clean_json = raw.replace("```json", "").replace("```", "").strip()
        match = re.search(r'\[.*\]', clean_json, re.DOTALL)
        if match: return json.loads(match.group()), raw
        return [], raw
    except Exception as e: return [], f"JSON Error: {str(e)}\nRaw: {raw}"

# =========================================================
# [5] UI: 메인 루프 (Wizard)
# =========================================================

with st.sidebar:
    st.markdown("### ⚙️ 시스템 설정")
    if st.session_state.debug_mode:
        if st.button("🐞 디버깅 모드 끄기"): st.session_state.debug_mode = False; st.rerun()
    else:
        st.markdown("<br>"*5, unsafe_allow_html=True)
        if st.button("🛠️ 관리자 모드 켜기"): st.session_state.debug_mode = True; st.rerun()

# ---------------------------------------------------------
# STEP 0: 시작 화면 (메인 디자인 복구)
# ---------------------------------------------------------
if st.session_state.step == 0:
    st.markdown("<h1 style='text-align: center; margin-bottom: 20px;'>📚 국어활동 AI 분석기</h1>", unsafe_allow_html=True)
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
            st.session_state.mode_key = "SOUTH"; st.session_state.step = 1; st.rerun()

    with c_north:
        st.markdown("""
        <style>
            div[data-testid="column"]:nth-of-type(3) div.stButton > button { border-color: rgba(255, 23, 68, 0.4) !important; box-shadow: 0 0 10px rgba(255, 23, 68, 0.1) !important; }
            div[data-testid="column"]:nth-of-type(3) div.stButton > button:hover { border-color: #ff1744 !important; box-shadow: 0 0 35px rgba(255, 23, 68, 0.7) !important; color: #ff1744 !important; }
        </style>
        """, unsafe_allow_html=True)
        if st.button("🏔️\n\n북한 문화어\n\n(문화어 규범 기준)\n\n[ 시작하기 ]", key="btn_north", use_container_width=True):
            st.session_state.mode_key = "NORTH"; st.session_state.step = 1; st.rerun()

# ---------------------------------------------------------
# STEP 1: 데이터 소스 선택
# ---------------------------------------------------------
elif st.session_state.step == 1:
    st.header("📂 데이터 소스 선택")
    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.subheader("📂 이어하기")
            up_excel = st.file_uploader("기존 분석 엑셀 파일 업로드", type=['xlsx'])
            if up_excel:
                try:
                    st.session_state.master_df = pd.read_excel(up_excel)
                    st.session_state.step = 2; st.rerun()
                except: st.error("엑셀 파일 형식이 올바르지 않습니다.")
    with col2:
        with st.container(border=True):
            st.subheader("🆕 새로 시작하기")
            if st.button("새 프로젝트 생성", use_container_width=True):
                st.session_state.master_df = None; st.session_state.step = 2; st.rerun()
    st.markdown("---")
    if st.button("⬅️ 모드 다시 선택"): st.session_state.step = 0; st.rerun()

# ---------------------------------------------------------
# STEP 2: 자료 입력 (상태 유지 및 초안 저장)
# ---------------------------------------------------------
elif st.session_state.step == 2:
    st.session_state.is_finished = False
    c_t, c_h = st.columns([8, 2])
    with c_t: st.header("📝 분석 자료 입력")
    with c_h:
        if st.button("🏠 처음으로"): st.session_state.clear(); st.rerun()

    input_method = st.radio("방식", ["📄 파일 분석", "✍️ 직접 입력"], horizontal=True, index=st.session_state.current_tab_idx, label_visibility="collapsed")

    def run_analysis_action(txt, img=None):
        if not txt.strip(): st.warning("분석할 내용이 없습니다."); return
        with st.spinner("AI 분석 중..."):
            s_data = get_sheet_data_fresh(st.session_state.mode_key)[1]
            res, raw = get_analysis_hybrid(txt, img, s_data, st.session_state.mode_key)
            st.session_state.last_raw_response = raw
            
            proc = []; temp_dict = {}
            om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
            pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사', '감탄사':'❗ 감탄사'}
            
            # [비교 학습을 위한 로직] 초안 가공 및 저장
            draft_items = []
            for r in res:
                o_word = str(r.get('원본') or '').strip()
                r_word = str(r.get('원형') or '').strip()
                origin_v = str(r.get('분류') or '혼').strip()
                pos_v = str(r.get('품사') or '명사').strip()
                
                # 초안 원본 데이터 보존 (학습 비교용)
                if o_word and r_word and re.search(r'[가-힣a-zA-Z0-9]', o_word):
                    if pos_v not in ['조사', '어미', '문장부호']:
                        draft_items.append({'원본': o_word, '원형': r_word, '분류': origin_v, '품사': pos_v})
                
                # 결과 테이블용 가공
                if not o_word or not r_word or not re.search(r'[가-힣a-zA-Z0-9]', o_word): continue
                if pos_v in ['조사', '어미', '문장부호']: continue

                key = (r_word, origin_v, pos_v)
                if key not in temp_dict: temp_dict[key] = []
                temp_dict[key].append(o_word)
            
            # 초안 세션 저장
            st.session_state.initial_draft = draft_items
            
            # 결과 세션 저장
            for (root, origin, pos), origs in temp_dict.items():
                cnts = Counter(origs)
                proc.append({
                    "삭제": False, "횟수": f"{sum(cnts.values())}회", 
                    "원본": ", ".join([f"{w}({c})" for w, c in cnts.items()]), 
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
                img = get_page_image(fb, file.type, st.session_state.page_idx)
                if img: st.image(img, use_container_width=True)
                if "pdf" in file.type:
                    if st.button("▶ 다음 페이지"): st.session_state.page_idx += 1; st.session_state.extracted_text = extract_text_unified(fb, file.type, st.session_state.page_idx); st.rerun()
                    st.session_state.start_offset = st.number_input("시작 쪽수", value=st.session_state.start_offset)
            with c2:
                txt_in = st.text_area("에디터", value=st.session_state.extracted_text, height=500)
                st.session_state.extracted_text = txt_in
                if st.button("🚀 분석 실행", type="primary", use_container_width=True): run_analysis_action(txt_in, fb)
    else:
        st.session_state.current_tab_idx = 1
        direct_t = st.text_area("텍스트 입력 창", value=st.session_state.extracted_text, height=450)
        st.session_state.extracted_text = direct_t
        if st.button("🚀 분석 실행", type="primary", use_container_width=True): run_analysis_action(direct_t)

# ---------------------------------------------------------
# STEP 3: 결과 확인 (비교 학습 저장 및 연속 작업 기능)
# ---------------------------------------------------------
elif st.session_state.step == 3:
    ch, cb = st.columns([8, 2])
    with ch: st.header("📊 분석 결과 확인")
    with cb:
        if st.button("⬅️ 입력 수정하기", use_container_width=True): st.session_state.step = 2; st.rerun()
    
    if st.session_state.debug_mode:
        with st.expander("🔴 [DEBUG] AI Raw Data"): st.code(st.session_state.last_raw_response)
    with st.expander("📝 분석 대상 원문 확인"):
        st.text_area("원문", value=st.session_state.extracted_text, height=200, disabled=True)

    dlg = st.dialog if hasattr(st, "dialog") else st.experimental_dialog
    @dlg("➕ 단어 추가")
    def add_manual():
        with st.form("manual_add"):
            o = st.text_input("원본 단어"); r = st.text_input("원형(기본형)")
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
                st.rerun()

    df_res = pd.DataFrame(st.session_state.analysis_result)
    edited = st.data_editor(
        df_res,
        column_config={
            "삭제": st.column_config.CheckboxColumn("삭제"),
            "원본": st.column_config.TextColumn("원본", disabled=True),
            "분류": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "품사": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사", "고유명사", "❗ 감탄사"])
        },
        use_container_width=True, num_rows="dynamic", key="editor_grid"
    )
    if not edited.equals(df_res): st.session_state.analysis_result = edited.to_dict('records')

    if not st.session_state.is_finished:
        b1, b2, b3 = st.columns([1, 1, 2])
        with b1:
            if st.button("➕ 단어 추가", use_container_width=True): add_manual()
        with b2:
            if st.button("⛔ 선택 삭제", use_container_width=True):
                st.session_state.analysis_result = edited[edited['삭제']==False].to_dict('records'); st.rerun()
        with b3:
            if st.button("💾 저장하기 (완료)", type="primary", use_container_width=True):
                save_logic_with_learning() # [핵심] 비교 학습 엔진 호출
                st.session_state.is_finished = True; st.balloons(); st.rerun()
    else:
        st.success("✅ 모든 분석 데이터가 성공적으로 비교 학습 및 저장되었습니다!")
        fname = f"Result_{st.session_state.mode_key}_{datetime.now().strftime('%m%d_%H%M')}.xlsx"
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as w: 
            st.session_state.master_df.to_excel(w, index=False)
            
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(label=f"📥 {fname} 다운로드", data=buf.getvalue(), file_name=fname, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, type="primary")
        with c2:
            if st.button("🔄 입력창으로 돌아가기 (연속 작업)", use_container_width=True):
                st.session_state.step = 2; st.session_state.is_finished = False; st.rerun()