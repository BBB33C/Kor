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
from datetime import datetime, timedelta # 시간 계산용 timedelta 추가
from collections import Counter
import traceback
from PIL import Image # 이미지 검증 및 재인코딩용 필수 라이브러리

# =========================================================
# [0] 기본 설정 및 라이브러리 초기화
# =========================================================
st.set_page_config(
    page_title="[TEST] 국어활동 AI 분석기",  # 제목 앞에 [TEST] 추가
    page_icon="🧪",                         # 아이콘 변경 (선택)
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ▼▼▼▼▼▼▼ [테스트 서버 전용 경고창 추가] ▼▼▼▼▼▼▼
st.markdown("""
    <div style='background-color: #ff4b4b; padding: 10px; border-radius: 5px; margin-bottom: 20px; text-align: center;'>
        <h3 style='color: white; margin: 0;'>🚧 현재 '테스트 서버(Test Server)' 접속 중입니다 🚧</h3>
        <p style='color: white; margin: 0;'>이곳에서의 작업은 실제 운영 서버에 영향을 주지 않습니다.</p>
    </div>
""", unsafe_allow_html=True)
# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲

# [Fix] 전역 변수 사전 초기화 (NameError 방지)
PLUMBER_AVAILABLE = False
FITZ_AVAILABLE = False

try:
    import pdfplumber
    PLUMBER_AVAILABLE = True
except ImportError:
    pass

try:
    import fitz 
    FITZ_AVAILABLE = True
except ImportError:
    pass

# 세션 상태 초기화
if 'step' not in st.session_state: st.session_state.step = 0
if 'mode_key' not in st.session_state: st.session_state.mode_key = None
if 'input_type' not in st.session_state: st.session_state.input_type = None
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'initial_draft' not in st.session_state: st.session_state.initial_draft = []
if 'file_bytes' not in st.session_state: st.session_state.file_bytes = None
if 'file_type' not in st.session_state: st.session_state.file_type = None
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'total_pages' not in st.session_state: st.session_state.total_pages = 1
if 'start_offset' not in st.session_state: st.session_state.start_offset = 1
if 'extracted_text' not in st.session_state: st.session_state.extracted_text = ""
if 'debug_mode' not in st.session_state: st.session_state.debug_mode = False
if 'last_raw_response' not in st.session_state: st.session_state.last_raw_response = ""
if 'debug_log' not in st.session_state: st.session_state.debug_log = ""
if 'is_finished' not in st.session_state: st.session_state.is_finished = False
if 'split_mode' not in st.session_state: st.session_state.split_mode = False # [New] 분할 모드 상태
if 'current_user' not in st.session_state: st.session_state.current_user = None # 현재 접속자 (아빠/엄마...)
if 'user_sheet_name' not in st.session_state: st.session_state.user_sheet_name = None # 저장될 시트 이름


# [New] 입력 데이터만 초기화하는 안전 함수
def reset_input_buffer():
    st.session_state.file_bytes = None
    st.session_state.file_type = None
    st.session_state.extracted_text = ""
    st.session_state.analysis_result = []
    st.session_state.initial_draft = []
    st.session_state.page_idx = 0
    st.session_state.total_pages = 1
    st.session_state.is_finished = False
    st.session_state.split_mode = False

# =========================================================
# [1] 디자인: CSS 매직
# =========================================================
if st.session_state.step in [0, 1.5]:
    st.markdown("""
        <style>
            .stApp { background-color: #0e1117 !important; color: #ffffff !important; }
            div.block-container div[data-testid="column"] div.stButton > button {
                width: 100%; height: 280px;
                background-image: none !important;
                background-color: #262730 !important;
                border: 2px solid rgba(255,255,255,0.1) !important;
                border-radius: 20px !important;
                color: #eeeeee !important;
                font-size: 1.4rem !important; font-weight: 700 !important;
                transition: all 0.3s ease !important;
                box-shadow: 0 4px 15px rgba(0,0,0,0.4) !important;
                white-space: pre-wrap !important;
            }
            div.block-container div[data-testid="column"] div.stButton > button:hover {
                transform: translateY(-10px);
                background-color: #2b2c36 !important;
                border-color: #2979ff !important;
            }
        </style>
    """, unsafe_allow_html=True)
elif st.session_state.step == 1:
    st.markdown("""
        <style>
            .stApp { background-color: #0e1117 !important; color: #ffffff !important; }
            div[data-testid="stVerticalBlockBorderWrapper"] {
                height: 350px !important;
                display: flex !important; 
                flex-direction: column !important; 
                justify-content: center !important;
            }
            div.stButton > button {
                width: 100%;
                background-image: none !important;
                background-color: #262730 !important;
                border: 2px solid rgba(255,255,255,0.1) !important;
                color: white !important;
                border-radius: 10px;
                height: 60px; font-size: 1.2rem; font-weight: bold;
                box-shadow: none !important;
            }
            div.stButton > button:hover {
                background-color: #2b2c36 !important;
                border-color: #2979ff !important;
                transform: translateY(-2px);
            }
        </style>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
        <style>
            .stApp { background-color: #0e1117 !important; color: #ffffff !important; }
            .stTextArea textarea { font-family: 'Malgun Gothic', sans-serif !important; font-size: 16px !important; line-height: 1.6 !important; }
            .stButton button { border-radius: 8px; font-weight: bold; height: auto; }
            .control-card { background-color: #1e2129; padding: 20px; border-radius: 15px; border: 1px solid #3d4251; margin-bottom: 20px; }
            .status-badge { background-color: #2979ff; padding: 4px 12px; border-radius: 20px; font-size: 0.9rem; font-weight: 600; color: white !important; }
            .info-card { background-color: rgba(41, 121, 255, 0.1); border-left: 5px solid #2979ff; padding: 15px; border-radius: 5px; margin-top: 15px; }
            .debug-box { background-color: #222; color: #00ff00; font-family: monospace; padding: 15px; border-radius: 5px; font-size: 0.85rem; overflow-x: auto; border: 1px solid #444; margin-top: 10px; white-space: pre-wrap; word-break: break-all; }
            .section-divider { border-bottom: 2px solid #3d4251; margin: 25px 0; }
            .guide-text { color: #888888; font-size: 0.85rem; font-weight: normal; margin-left: 10px; }
            [data-testid="stImage"] { margin-bottom: -15px !important; }
        </style>
    """, unsafe_allow_html=True)

# =========================================================
# [2] 구글 시트 및 API 엔진
# =========================================================
try:
    if "GEMINI_API_KEY" in st.secrets: API_KEY = st.secrets["GEMINI_API_KEY"]
    else: API_KEY = ""
except: API_KEY = ""

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

@st.cache_data(show_spinner=False)
def fetch_all_rules_from_db(mode_key):
    client = get_google_sheet_client()
    if not client: return []
    target = "South_Korea" if mode_key == "SOUTH" else "North_Korea"
    try:
        sh = client.open(SHEET_NAME)
        ws = sh.worksheet(target)
        return ws.get_all_records()
    except: return []

def get_sheet_object_for_write(mode_key):
    client = get_google_sheet_client()
    if not client: return None
    target = "South_Korea" if mode_key == "SOUTH" else "North_Korea"
    try:
        sh = client.open(SHEET_NAME)
        try: ws = sh.worksheet(target)
        except: 
            ws = sh.add_worksheet(title=target, rows=1000, cols=20)
            ws.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context", "initial_root", "initial_origin"])
        return ws
    except: return None

def send_data_with_retry(sheet_obj, data, is_multiple=False):
    if not sheet_obj: return False
    for _ in range(3):
        try:
            if is_multiple: sheet_obj.append_rows([[str(i) for i in r] for r in data])
            else: sheet_obj.append_row([str(i) for i in data])
            return True
        except: time.sleep(1)
    return False

# [수정] 언어가 아니라 '사용자 이름'으로 된 시트에 저장하도록 변경
def save_backup_to_cloud(mode_key, df):
    client = get_google_sheet_client()
    if not client or df is None or df.empty: return False
    
    # 사용자가 선택되지 않았다면 기본값(Default) 사용
    target_sheet = st.session_state.get('user_sheet_name', f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}")
    
    try:
        sh = client.open(SHEET_NAME)
        # 해당 사용자의 시트가 없으면 생성, 있으면 열기
        try: ws = sh.worksheet(target_sheet)
        except: ws = sh.add_worksheet(title=target_sheet, rows=1000, cols=20)
        
        ws.clear()
        # 데이터프레임 저장 (메타데이터로 모드 정보도 어딘가에 넣으면 좋지만, 일단 데이터부터 저장)
        ws.update([df.fillna("").astype(str).columns.tolist()] + df.fillna("").astype(str).values.tolist())
        return True
    except: return False

# [추가] 사용자의 백업 데이터를 불러오는 함수
def load_backup_from_cloud():
    client = get_google_sheet_client()
    if not client or not st.session_state.user_sheet_name: return None
    try:
        sh = client.open(SHEET_NAME)
        ws = sh.worksheet(st.session_state.user_sheet_name)
        data = ws.get_all_records()
        if data: return pd.DataFrame(data)
    except: pass
    return None

# =========================================================
# [3] 데이터 병합 및 비교 학습 엔진
# =========================================================
def clean_val_for_save(v):
    if isinstance(v, str): 
        chars_to_remove = ['🔵 ', '🟢 ', '🔴 ', '🟣 ', '📦 ', '🏃 ', '🎨 ', '⚡ ', '🔍 ', '👤 ', '❗ ']
        for char in chars_to_remove: v = v.replace(char, '')
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
    
    fixed_cols = ['구분', '자료', '출연횟수']
    page_cols = sorted([c for c in result_df.columns if c.startswith('쪽수')], key=lambda x: int(re.sub(r'[^0-9]', '', x)) if re.search(r'\d', x) else 9999)
    final_cols = fixed_cols + page_cols
    remaining_cols = [c for c in result_df.columns if c not in final_cols and c != 'sk']
    final_cols = final_cols + remaining_cols
    
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    result_df['sk'] = result_df['구분'].map(sort_map).fillna(5)
    result_df = result_df.sort_values(['sk', '자료']).drop('sk', axis=1)
    result_df = result_df.reindex(columns=final_cols)
    result_df = result_df.fillna("")
    return result_df

def save_logic_with_learning():
    sheet = get_sheet_object_for_write(st.session_state.mode_key)
    now = datetime.now().isoformat()
    learning_logs = []
    final_results = pd.DataFrame(st.session_state.analysis_result)
    initial_draft = pd.DataFrame(st.session_state.initial_draft)
    
    for _, draft_row in initial_draft.iterrows():
        orig = draft_row['원본']
        match = final_results[final_results['원본'] == orig]
        if match.empty or match.iloc[0]['삭제']:
            learning_logs.append([now, orig, draft_row['원형'], draft_row['분류'], draft_row['품사'], 'delete', 'Engine-Compare', draft_row['원형'], draft_row['분류']])
        else:
            final_row = match.iloc[0]
            if (draft_row['원형'] != final_row['원형'] or clean_val_for_save(draft_row['분류']) != clean_val_for_save(final_row['분류']) or clean_val_for_save(draft_row['품사']) != clean_val_for_save(final_row['품사'])):
                learning_logs.append([now, orig, final_row['원형'], clean_val_for_save(final_row['분류']), clean_val_for_save(final_row['품사']), 'modify', 'Engine-Compare', draft_row['원형'], draft_row['분류']])
    
    draft_originals = initial_draft['원본'].tolist()
    for _, final_row in final_results.iterrows():
        if final_row['원본'] not in draft_originals and not final_row['삭제']:
            learning_logs.append([now, final_row['원본'], final_row['원형'], clean_val_for_save(final_row['분류']), clean_val_for_save(final_row['품사']), 'add', 'Engine-New', '', ''])
    
    if learning_logs: 
        send_data_with_retry(sheet, learning_logs, True)
        fetch_all_rules_from_db.clear()
    
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
# [4] AI 분석 및 이미지 최적화 처리
# =========================================================
def clean_raw_text(text):
    text = re.sub(r'.*\.indd.*', '', text)
    text = re.sub(r'\d{4}-\d{2}-\d{2}', '', text)
    text = re.sub(r'(오전|오후)\s+\d{1,2}:\d{2}:\d{2}', '', text)
    text = re.sub(r'본문\d?\(.*\)\d{2}', '', text)
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return '\n'.join(lines)

def process_image_for_api(image_bytes):
    if not image_bytes: return None
    try:
        img_io = io.BytesIO(image_bytes)
        img = Image.open(img_io)
        if img.mode != "RGB": img = img.convert("RGB")
        if max(img.size) > 3000: img.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        img.save(output, format="PNG")
        return output.getvalue()
    except Exception as e: return None

def api_call_direct(prompt, image_bytes=None):
    if not API_KEY: return None, "API Key Missing"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    parts = [{"text": prompt}]
    
    if image_bytes:
        optimized_img = process_image_for_api(image_bytes)
        if optimized_img:
            b64_img = base64.b64encode(optimized_img).decode('utf-8')
            parts.append({"inline_data": {"mime_type": "image/png", "data": b64_img}})
            
    try:
        res = requests.post(url, headers=headers, json={"contents": [{"parts": parts}]}, timeout=300)
        if res.status_code == 200: 
            return res.json()['candidates'][0]['content']['parts'][0]['text'], "Success"
        return None, f"Error: {res.status_code} - {res.text}"
    except Exception as e: return None, str(e)

def extract_text_unified(file_bytes, file_type, page_idx):
    if not file_type: return ""
    raw_text = ""
    
    if "image" in file_type: 
        raw_text, _ = api_call_direct("이 이미지 속의 텍스트를 모두 추출하세요. 줄바꿈 유지.", file_bytes)
    elif "pdf" in file_type:
        if FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                # [Update] 분할 모드일 때 페이지 수 처리
                total = len(doc) * 2 if st.session_state.split_mode else len(doc)
                st.session_state.total_pages = total
            except: pass
        elif PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    total = len(pdf.pages) * 2 if st.session_state.split_mode else len(pdf.pages)
                    st.session_state.total_pages = total
            except: pass
            
        page_img = get_page_image(file_bytes, file_type, page_idx)
        if page_img:
            raw_text, _ = api_call_direct("이 이미지 속의 텍스트를 모두 추출하세요. 줄바꿈 유지.", page_img)
        else:
            if PLUMBER_AVAILABLE:
                try:
                    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                        # Fallback: 분할 모드 미지원 (Vision이 메인)
                        target_idx = page_idx // 2 if st.session_state.split_mode else page_idx
                        if target_idx < len(pdf.pages): raw_text = pdf.pages[target_idx].extract_text()
                except: pass
    return clean_raw_text(raw_text or "")

def get_page_image(file_bytes, file_type, page_idx):
    if not file_bytes or not file_type: return None
    if "image" in file_type: return file_bytes
    if "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            
            # [Update] 분할 모드 로직 적용
            is_split = st.session_state.split_mode
            pdf_page_idx = page_idx // 2 if is_split else page_idx
            is_right_half = (page_idx % 2 == 1)
            
            if pdf_page_idx < len(doc): 
                # PDF 화질 4배 (Vision 정확도용)
                pix = doc[pdf_page_idx].get_pixmap(matrix=fitz.Matrix(4.0, 4.0))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                
                if is_split:
                    # 반으로 자르기 (Crop)
                    w, h = img.size
                    if is_right_half:
                        img = img.crop((w//2, 0, w, h)) # 오른쪽 반
                    else:
                        img = img.crop((0, 0, w//2, h)) # 왼쪽 반
                
                # 이미지 다시 바이트로 변환
                output = io.BytesIO()
                img.save(output, format="PNG")
                return output.getvalue()
        except: return None
    return None

def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rule_dict = {}
    for row in sheet_data:
        orig = str(row.get('original_word', '')).strip()
        if not orig: continue
        rule_dict[orig] = row

    rules = []
    for orig, row in rule_dict.items():
        action = row.get('action', '')
        root = row.get('root_word', '')
        origin = row.get('origin', '')
        pos = row.get('pos', '')
        
        if action == 'delete':
            rules.append(f"- '{orig}'는 추출 제외.")
        elif action in ['modify', 'add']:
            rules.append(f"- '{orig}' 정답: 원형:'{root}', 어종:'{origin}', 품사:'{pos}'.")
            
    return "\n[사용자 교정 데이터 (최우선 준수)]:\n" + "\n".join(rules) + "\n"

def run_analysis_action(txt, img_bytes=None):
    if not txt.strip(): st.warning("내용이 없습니다."); return
    
    with st.spinner("AI가 국어학적 관점에서 정밀 분석 중입니다..."):
        s_data = fetch_all_rules_from_db(st.session_state.mode_key)
        
        # [Update] 과잉 교정 방지 프롬프트 (Transcription First)
        prompt = f"""
        당신은 국어학 및 시맨틱 텍스트 분석 전문가입니다. 
        
        [절대 규칙 - Transcription First]
        1. **'원본'**은 이미지나 텍스트에 있는 **'어절(Word Segment)'**을 토씨 하나 틀리지 말고 그대로 옮겨 적으십시오.
        2. 오타, 띄어쓰기 오류, 활용된 어미, 조사 모두 **보이는 그대로** 적어야 합니다. 절대 임의로 수정하거나 기본형으로 바꾸지 마십시오.
        3. **'원형'**과 **'품사'**는 그 '원본'을 보고 언어학적으로 분석하여 채우십시오.
        
        {generate_prompt_from_sheet(s_data)}
        
        [예시 - 반드시 이 형식을 따를 것]
        * 텍스트: "선생님께서 말씀하셨습니다."
          -> {{ "원본": "말씀하셨습니다", "원형": "말씀하다", "품사": "동사", "분류": "고" }}
        * 텍스트: "친구랑 학교에 갔다"
          -> {{ "원본": "친구랑", "원형": "친구", "품사": "명사", "분류": "고" }}
          -> {{ "원본": "갔다", "원형": "가다", "품사": "동사", "분류": "고" }}
        * 텍스트: "시작합니다"
          -> {{ "원본": "시작합니다", "원형": "시작하다", "품사": "동사", "분류": "한" }} (O)
          -> {{ "원본": "시작하다", "원형": "시작하다", ... }} (X - 원본 변형 금지)

        [1. 고유명사(Named Entity) 처리]
        - **인명, 지명 등 고유명사**는 특별한 표시 없이 원형 그대로 출력하십시오.
        - 품사는 반드시 **'명사'**로 통일하십시오.
        
        [2. 동음이의어(Homonym) 구분]
        - 단어의 형태가 같으나 뜻이 다른 경우만 괄호로 구분. (예: 배(과일), 배(선박))
        
        [3. 용언(동사/형용사) 기본형]
        - 반드시 어미 '다'를 붙일 것. (예: 했다 -> 하다, 예쁜 -> 예쁘다)
        
        [4. 제외 대상]
        - 조사, 어미, 수사, 숫자, 특수기호.
        - **의존 명사**: 것, 수, 데, 바, 만큼, 지, 등, 뿐, 따름 등 제외.
        
        [5. 어종] 고(순우리말), 한(한자어), 외(외래어), 혼(혼종어).
        
        [출력 양식: JSON 리스트]
        [
          {{"원본": "보이는그대로", "원형": "기본형", "분류": "고/한/외/혼", "품사": "명사/동사/..."}},
          ...
        ]
        """
        
        raw, status = api_call_direct(prompt + f"\n\n[분석 대상]:\n{txt[:5000]}", img_bytes)
        st.session_state.last_raw_response = raw
        
        try:
            if not raw: raise Exception("API 응답이 비어있습니다.")
            clean_json = raw.replace("```json", "").replace("```", "").strip()
            match = re.search(r'\[.*\]', clean_json, re.DOTALL)
            
            if match: res = json.loads(match.group())
            else:
                try: res = json.loads(clean_json)
                except: 
                    st.warning("텍스트가 인식되지 않았습니다. 다시 한번 시도해주세요.")
                    res = []

            proc = []; temp_dict = {}
            om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
            pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사', '감탄사':'❗ 감탄사'}
            
            draft_items = []
            for r in res:
                o, root = str(r.get('원본') or '').strip(), str(r.get('원형') or '').strip()
                orig_v, pos_v = str(r.get('분류') or '혼').strip(), str(r.get('품사') or '명사').strip()
                
                if re.search(r'[0-9a-zA-Z]', o) or re.search(r'[0-9a-zA-Z]', root): continue
                if not o or not root: continue
                if pos_v in ['조사', '어미', '의존명사', '의존 명사', '수사']: continue
                if root in ['것', '수', '데', '바', '지', '리', '개', '번', '명', '쪽', '등', '따름', '뿐']: continue
                
                draft_items.append({'원본': o, '원형': root, '분류': orig_v, '품사': pos_v})
                key = (root, orig_v, pos_v)
                if key not in temp_dict: temp_dict[key] = []
                temp_dict[key].append(o)
                
            st.session_state.initial_draft = draft_items
            for (root, origin, pos), origs in temp_dict.items():
                cnts = Counter(origs)
                proc.append({"삭제": False, "횟수": f"{sum(cnts.values())}회", "원본": ", ".join([f"{w}({c})" for w, c in cnts.items()]), "원형": root, "분류": om.get(origin, origin), "품사": pm.get(pos, pos)})
            
            st.session_state.analysis_result = proc; st.session_state.step = 3; st.rerun()
            
        except Exception as e:
            st.error(f"파싱 오류: {str(e)}")
            st.session_state.debug_log = f"Error: {str(e)}\nRaw Response:\n{raw}"

# =========================================================
# [5] UI: 메인 루프 (Wizard)
# =========================================================

with st.sidebar:
    st.markdown("### ⚙️ 시스템 설정")
    if st.session_state.debug_mode:
        if st.button("🐞 디버깅 모드 끄기"): st.session_state.debug_mode = False; st.rerun()
    else:
        st.markdown("<br>"*5, unsafe_allow_html=True)
        if st.button("🛠️ 관리자/디버깅 모드 켜기"): st.session_state.debug_mode = True; st.rerun()

# =========================================================
# [Step 0] 가족 프로필 선택 (넷플릭스 스타일)
# =========================================================
if st.session_state.step == 0:
    st.markdown("<h1 style='text-align: center; margin-bottom: 30px;'>👨‍👩‍👧‍👦 작업자를 선택해주세요</h1>", unsafe_allow_html=True)
    
    # 4인 가족 프로필 버튼
    c1, c2, c3, c4 = st.columns(4)
    
    def set_user(name, sheet, icon):
        st.session_state.current_user = name
        st.session_state.user_sheet_name = sheet
        st.session_state.step = 0.5 # 대시보드로 이동
        st.rerun()

    with c1:
        if st.button("👨\n\n아빠", use_container_width=True): set_user("아빠", "Backup_Dad", "👨")
    with c2:
        if st.button("👩\n\n엄마", use_container_width=True): set_user("엄마", "Backup_Mom", "👩")
    with c3:
        if st.button("👧\n\n누나", use_container_width=True): set_user("누나", "Backup_Sis", "👧")
    with c4:
        if st.button("👦\n\n동생", use_container_width=True): set_user("동생", "Backup_Bro", "👦")

# =========================================================
# [Step 0.5] 개인 대시보드 (이어하기 / 목록 / 새로하기)
# =========================================================
elif st.session_state.step == 0.5:
    st.markdown(f"### 👋 안녕하세요, {st.session_state.current_user}님!")
    
    # 1. 백업 데이터 확인
    backup_df = load_backup_from_cloud()
    has_backup = backup_df is not None and not backup_df.empty
    
    col_main, col_side = st.columns([2, 1])
    
    with col_main:
        # [A] 이어하기 카드
        st.markdown("#### ⏯️ 최근 작업 이어하기")
        if has_backup:
            # 마지막 작업 정보 추출 (예시로 쪽수 확인)
            last_page = "정보 없음"
            if '쪽수1' in backup_df.columns:
                last_page = str(backup_df.iloc[-1]['쪽수1'])
            
            st.info(f"💾 **저장된 작업 발견:** 마지막 작업 페이지 ({last_page}쪽) 등 데이터가 있습니다.")
            
            if st.button("🚀 저장된 내용 불러오기 (이어하기)", type="primary", use_container_width=True):
                st.session_state.master_df = backup_df
                # [주의] 여기서 모드(남/북)를 몰라서 일단 남한말로 가정하거나, 
                # 저장할 때 모드 정보를 같이 저장했어야 함. 임시로 선택창 띄움.
                st.session_state.step = 0.8 # 언어 모드 확인 단계로 이동
                st.rerun()
        else:
            st.warning("📭 저장된 최근 작업이 없습니다.")

        st.markdown("---")

        # [B] 새 프로젝트
        st.markdown("#### ✨ 새로운 작업 시작")
        if st.button("📄 새 프로젝트 만들기 (데이터 초기화)", use_container_width=True):
            reset_input_buffer()
            st.session_state.master_df = None
            st.session_state.step = 0.8 # 언어 선택으로 이동
            st.rerun()
            
    with col_side:
        if st.button("🔒 로그아웃 (프로필 다시 선택)", use_container_width=True):
            reset_input_buffer()
            st.session_state.current_user = None
            st.session_state.step = 0
            st.rerun()

# =========================================================
# [Step 0.8] 언어 모드 선택 (이어하기/새로하기 공통)
# =========================================================
elif st.session_state.step == 0.8:
    st.markdown(f"### 🌐 분석할 언어 규범을 선택하세요 ({st.session_state.current_user}님)")
    
    c_south, c_north = st.columns(2)
    with c_south:
        if st.button("🏛️ 대한민국 표준어", use_container_width=True):
            st.session_state.mode_key = "SOUTH"
            st.session_state.step = 1.5 # 바로 입력 방식 선택으로 점프
            st.rerun()
    with c_north:
        if st.button("🏔️ 북한 문화어", use_container_width=True):
            st.session_state.mode_key = "NORTH"
            st.session_state.step = 1.5 # 바로 입력 방식 선택으로 점프
            st.rerun()

# STEP 2: 자료 입력
elif st.session_state.step == 2:
    st.session_state.is_finished = False
    
    c_head, c_nav = st.columns([8, 2])
    title_text = "TEXT 분석 자료 입력" if st.session_state.input_type == "DIRECT" else f"{st.session_state.input_type} 분석 자료 입력"
    with c_head: st.header(f"📝 {title_text}")
    with c_nav:
        if st.button("🏠 처음으로", use_container_width=True): 
            reset_input_buffer()
            st.session_state.step = 0; st.rerun()
    
    with st.expander("⚙️ 쪽수 및 환경 설정", expanded=True):
        st.session_state.start_offset = st.number_input("현재 작업 중인 페이지의 쪽수 설정 (PDF는 시작 쪽수)", value=st.session_state.start_offset)
        actual_p = st.session_state.page_idx + st.session_state.start_offset
        st.markdown(f"<div class='info-card'>💾 <b>저장 위치:</b> 현재 작업 중인 내용은 엑셀의 <b>'{actual_p}쪽'</b>으로 기록됩니다.</div>", unsafe_allow_html=True)

    if st.session_state.input_type == "PDF":
        # [New] 분할 모드 체크박스
        st.session_state.split_mode = st.checkbox("✅ 두 쪽 모아찍기 문서 (좌우 분할 모드)", value=st.session_state.split_mode)
        
        file = st.file_uploader("PDF 파일 업로드", type=['pdf'])
        effective_file = None
        if file:
            effective_file = file.getvalue()
            if st.session_state.file_bytes != effective_file:
                st.session_state.file_bytes = effective_file
                st.session_state.file_type = "application/pdf"
                st.session_state.page_idx = 0
                if FITZ_AVAILABLE:
                    try: 
                        with fitz.open(stream=effective_file, filetype="pdf") as doc: 
                            # 분할 모드 반영하여 총 페이지 수 계산
                            st.session_state.total_pages = len(doc) * 2 if st.session_state.split_mode else len(doc)
                    except: pass
                st.session_state.extracted_text = extract_text_unified(effective_file, "application/pdf", 0)
        elif st.session_state.file_bytes:
            effective_file = st.session_state.file_bytes
            # 체크박스 변경 시 페이지 수 재계산 로직
            if FITZ_AVAILABLE:
                try:
                    with fitz.open(stream=effective_file, filetype="pdf") as doc:
                        st.session_state.total_pages = len(doc) * 2 if st.session_state.split_mode else len(doc)
                except: pass

        if effective_file:
            c1, c2 = st.columns(2)
            with c1:
                img = get_page_image(st.session_state.file_bytes, "application/pdf", st.session_state.page_idx)
                if img: st.image(img, use_container_width=True)
                st.markdown(f"<div style='margin-top:-10px; margin-bottom:10px;'><span class='status-badge'>📄 PDF 상태: {st.session_state.page_idx+1} / {st.session_state.total_pages} 페이지</span></div>", unsafe_allow_html=True)
                
                j1, j2, j3 = st.columns([1,1,1])
                with j1: 
                    if st.button("◀ 이전", use_container_width=True, disabled=st.session_state.page_idx<=0):
                        st.toast("⏳ 페이지 분석 중입니다... 잠시만 기다려주세요.", icon="📄")
                        st.session_state.page_idx -= 1
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, "application/pdf", st.session_state.page_idx)
                        st.rerun()
                with j2: 
                    target_p = st.number_input("이동", 1, st.session_state.total_pages, st.session_state.page_idx+1, label_visibility="collapsed")
                    if target_p != st.session_state.page_idx + 1:
                        st.toast("⏳ 페이지 이동 중입니다...", icon="🚀")
                        st.session_state.page_idx = target_p - 1
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, "application/pdf", st.session_state.page_idx)
                        st.rerun()
                with j3:
                    if st.button("다음 ▶", use_container_width=True, disabled=st.session_state.page_idx>=st.session_state.total_pages-1):
                        st.toast("⏳ 페이지 분석 중입니다... 잠시만 기다려주세요.", icon="📄")
                        st.session_state.page_idx += 1
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, "application/pdf", st.session_state.page_idx)
                        st.rerun()
            with c2:
                st.session_state.extracted_text = st.text_area("에디터 (추출 텍스트)", value=st.session_state.extracted_text, height=520)
                if st.button("🚀 분석 실행", type="primary", use_container_width=True): run_analysis_action(st.session_state.extracted_text, st.session_state.file_bytes)

    elif st.session_state.input_type == "IMAGE":
        file = st.file_uploader("이미지 파일 업로드", type=['png', 'jpg', 'jpeg'])
        effective_file = None
        if file:
            effective_file = file.getvalue()
            if st.session_state.file_bytes != effective_file:
                st.session_state.file_bytes = effective_file
                st.session_state.file_type = "image/png"
                st.session_state.extracted_text = extract_text_unified(effective_file, "image/png", 0)
        elif st.session_state.file_bytes and st.session_state.input_type == "IMAGE":
            effective_file = st.session_state.file_bytes

        if effective_file:
            c1, c2 = st.columns(2)
            with c1:
                try:
                    if "image" in str(st.session_state.file_type):
                        st.image(st.session_state.file_bytes, use_container_width=True, caption="이미지 원본")
                except:
                    st.error("이미지를 표시할 수 없습니다. (파일 형식 오류)")
            with c2:
                st.session_state.extracted_text = st.text_area("에디터 (추출 텍스트)", value=st.session_state.extracted_text, height=520)
                if st.button("🚀 분석 실행", type="primary", use_container_width=True): run_analysis_action(st.session_state.extracted_text, st.session_state.file_bytes)

    elif st.session_state.input_type == "DIRECT":
        st.session_state.extracted_text = st.text_area("분석할 텍스트를 입력하세요", value=st.session_state.extracted_text, height=450)
        if st.button("🚀 분석 실행", type="primary", use_container_width=True): run_analysis_action(st.session_state.extracted_text, None)

# STEP 3: 결과 확인
elif st.session_state.step == 3:
    actual_p = st.session_state.page_idx + st.session_state.start_offset
    
    ch, cb = st.columns([8.5, 1.5])
    with ch: 
        st.header("📊 분석 결과 확인")
        st.markdown(f"<p style='color: #2979ff; font-size: 0.95rem; margin-top:-15px;'>[{actual_p}쪽으로 엑셀에 저장 예정]</p>", unsafe_allow_html=True)
    with cb:
        if st.button("⬅️ 입력 \n 창으로", use_container_width=True): st.session_state.step = 2; st.rerun()
    
    if st.session_state.debug_mode:
        with st.expander("🛠️ [정밀 디버깅] 로그 확인", expanded=True):
            st.markdown(f"<div class='debug-box'>{st.session_state.get('debug_log', '로그 없음')}</div>", unsafe_allow_html=True)
            if st.session_state.last_raw_response:
                st.markdown("**AI Raw Response:**")
                st.code(st.session_state.last_raw_response)

    if st.session_state.input_type in ["PDF", "IMAGE"]:
        c1, c2 = st.columns([1, 1])
        with c1:
            try:
                img = get_page_image(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                if img:
                    cap = f"📄 PDF 총 {st.session_state.total_pages}쪽 중 현재 {st.session_state.page_idx+1}쪽 (📍 엑셀에 '{actual_p}쪽' 저장 예정)" if st.session_state.input_type=="PDF" else "이미지 미리보기"
                    st.image(img, use_container_width=True, caption=cap)
            except: pass
        with c2:
            st.text_area("추출 원문 확인", value=st.session_state.extracted_text, height=500, disabled=True)
    else: 
        st.text_area("입력 원문 확인", value=st.session_state.extracted_text, height=250, disabled=True)

    st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
    st.markdown("### 📋 분석 결과 편집 <span class='guide-text'>(※ 동작 직후 데이터 동기화 중 알림이 사라지면 다음 동작을 진행해주세요)</span>", unsafe_allow_html=True)
    
    df_res = pd.DataFrame(st.session_state.analysis_result)
    if not df_res.empty:
        edited = st.data_editor(df_res, column_config={
            "삭제": st.column_config.CheckboxColumn("삭제"),
            "원본": st.column_config.TextColumn("원본", disabled=True),
            "분류": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "품사": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사", "고유명사", "❗ 감탄사"])
        }, use_container_width=True, num_rows="dynamic", key="editor_final")
        
        if not edited.equals(df_res):
            diff_mask = (edited != df_res).any(axis=1)
            if not edited[diff_mask][[c for c in df_res.columns if c != "삭제"]].equals(df_res[diff_mask][[c for c in df_res.columns if c != "삭제"]]):
                st.session_state.analysis_result = edited.to_dict('records')
                st.toast("🔄 데이터 동기화 중..."); time.sleep(2.0); st.rerun()
            else: st.session_state.analysis_result = edited.to_dict('records')
    else:
        st.warning("⚠️ 분석된 단어가 없거나 모두 필터링되었습니다. 원문을 확인하거나 직접 단어를 추가해주세요.")
    
    @st.dialog("➕ 단어 직접 추가")
    def open_add_dialog():
        with st.form("manual_add_form"):
            o = st.text_input("원본 단어")
            r = st.text_input("원형(기본형)")
            org = st.selectbox("어종 분류", ["고","한","외","혼"])
            p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사","고유명사","감탄사"])
            cnt = st.number_input("출연 횟수", 1, 100, 1)
            if st.form_submit_button("추가 완료"):
                st.session_state.analysis_result.append({
                    "삭제": False, "횟수": f"{cnt}회", "원본": f"{o}(수동)", "원형": r, 
                    "분류": {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}.get(org, org), 
                    "품사": {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사', '감탄사':'❗ 감탄사'}.get(p, p)
                })
                st.toast("✅ 단어 추가 완료. 동기화 중...", icon="✨")
                time.sleep(2.0)
                st.rerun()

    if not st.session_state.is_finished:
        if st.session_state.input_type in ["DIRECT", "IMAGE"]:
            b1, b2, b3 = st.columns([1, 1, 2])
            with b1: 
                if st.button("➕ 단어 추가", use_container_width=True): open_add_dialog()
            with b2:
                if st.button("⛔ 선택 삭제", use_container_width=True):
                    st.session_state.analysis_result = [r for r in st.session_state.analysis_result if not r.get('삭제', False)]
                    st.toast("🗑️ 삭제 데이터 정리 중..."); time.sleep(2.0); st.rerun()
            with b3:
                if st.button("💾 결과 저장 및 학습", type="primary", use_container_width=True):
                    with st.status("데이터 저장 및 학습 반영 중..."):
                        save_logic_with_learning()
                        st.session_state.is_finished = True
                        st.success("✅ 저장이 완료되었습니다!")
                        time.sleep(1.0)
                        st.rerun()
        else:
            b1, b2, b3, b4 = st.columns([1, 1, 1.5, 2])
            with b1: 
                if st.button("➕ 단어 추가", use_container_width=True): open_add_dialog()
            with b2:
                if st.button("⛔ 선택 삭제", use_container_width=True):
                    st.session_state.analysis_result = [r for r in st.session_state.analysis_result if not r.get('삭제', False)]
                    st.toast("🗑️ 삭제 데이터 정리 중..."); time.sleep(2.0); st.rerun()
            with b3:
                if st.button("💾 현재 페이지만 저장", use_container_width=True):
                    save_logic_with_learning(); st.session_state.is_finished = True; st.rerun()
            with b4:
                if st.button("🚀 저장하고 다음 쪽 가기", type="primary", use_container_width=True):
                    save_logic_with_learning()
                    if st.session_state.input_type == "PDF" and st.session_state.page_idx < st.session_state.total_pages - 1:
                        st.toast("⏳ 다음 페이지 분석 중...", icon="📄")
                        st.session_state.page_idx += 1; st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, "application/pdf", st.session_state.page_idx)
                        st.session_state.analysis_result = []; st.session_state.step = 2; st.rerun()
                    else: st.session_state.is_finished = True; st.balloons(); st.rerun()
    else:
        st.success("✅ 저장이 완료되었습니다!")
        kst_now = datetime.utcnow() + timedelta(hours=9)
        fname = f"KR 분석 결과 {kst_now.strftime('%m%d_%H%M')}.xlsx"
        buf = io.BytesIO()
        try:
            with pd.ExcelWriter(buf, engine='openpyxl') as w: 
                st.session_state.master_df.astype(str).to_excel(w, index=False)
        except:
            with pd.ExcelWriter(buf, engine='openpyxl') as w: 
                st.session_state.master_df.to_excel(w, index=False)
        
        c_down, c_next = st.columns([1, 1])
        with c_down:
            st.download_button(label=f"📥 엑셀 다운로드", data=buf.getvalue(), file_name=fname, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, type="primary")
        
        if st.session_state.input_type == "PDF":
            with c_next:
                can_go_next = st.session_state.file_type and "pdf" in st.session_state.file_type and st.session_state.page_idx < st.session_state.total_pages - 1
                if st.button("➡️ 다음 쪽으로 이동", use_container_width=True, disabled=not can_go_next):
                    st.toast("⏳ 다음 페이지 분석 중...", icon="📄")
                    st.session_state.page_idx += 1
                    st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                    st.session_state.analysis_result = []
                    st.session_state.step = 2
                    st.session_state.is_finished = False
                    st.rerun()