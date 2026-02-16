import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import fitz
import os
import time
import base64
import hashlib
import gspread
import numpy as np
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta # 시간 계산용 timedelta 추가
from collections import Counter
import traceback
from PIL import Image # 이미지 검증 및 재인코딩용 필수 라이브러리

# =========================================================
# [0] 기본 설정 및 라이브러리 초기화
# =========================================================
st.set_page_config(
    page_title="국어활동 AI 분석기", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="collapsed"
)

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

MODEL_NAME = "gemini-2.5-pro"
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
# [3] 데이터 병합 및 비교 학습 엔진 (수정됨)
# =========================================================
def clean_val_for_save(v):
    try:
        if isinstance(v, str): 
            chars_to_remove = ['🔵 ', '🟢 ', '🔴 ', '🟣 ', '📦 ', '🏃 ', '🎨 ', '⚡ ', '🔍 ', '👤 ', '❗ ']
            for char in chars_to_remove: v = v.replace(char, '')
            return v.strip()
        return str(v)
    except: return ""

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
    # 1. 빈 데이터 방어 (빈 껍데기라도 반환해야 엑셀 에러 안 남)
    if (old_df is None or old_df.empty) and (new_df is None or new_df.empty):
        return pd.DataFrame(columns=['구분', '자료', '출연횟수', '쪽수1'])
    
    # 2. 하나만 있으면 그거 반환
    if old_df is None or old_df.empty: return new_df
    if new_df is None or new_df.empty: return old_df
    
    try:
        # 3. 안전한 병합을 위해 문자열로 변환
        old_df['자료'] = old_df['자료'].astype(str)
        new_df['자료'] = new_df['자료'].astype(str)
        old_df['구분'] = old_df['구분'].astype(str)
        new_df['구분'] = new_df['구분'].astype(str)

        # 4. Melt & Concat 방식 (인덱스 충돌 방지)
        page_cols_old = [c for c in old_df.columns if str(c).startswith('쪽수')]
        page_cols_new = [c for c in new_df.columns if str(c).startswith('쪽수')]
        
        melted_old = old_df.melt(id_vars=['자료', '구분'], value_vars=page_cols_old, value_name='page').dropna()
        melted_new = new_df.melt(id_vars=['자료', '구분'], value_vars=page_cols_new, value_name='page').dropna()
        
        combined = pd.concat([melted_old, melted_new], ignore_index=True)
        
        # 5. 쪽수 정제
        combined['page'] = combined['page'].astype(str).str.strip()
        combined = combined[~combined['page'].isin(['nan', '', 'None'])]
        
        # 6. GroupBy로 합치기
        grouped = combined.groupby(['자료', '구분'])['page'].apply(lambda x: sorted(list(set(x)))).reset_index()
        
        # 7. 결과 생성
        result_rows = []
        for _, row in grouped.iterrows():
            pages = row['page']
            base_data = {
                '구분': row['구분'],
                '자료': row['자료'],
                '출연횟수': len(pages)
            }
            for i, p in enumerate(pages):
                base_data[f'쪽수{i+1}'] = p
            result_rows.append(base_data)
            
        return pd.DataFrame(result_rows)

    except Exception as e:
        print(f"Merge Error: {e}")
        return new_df

def save_logic_with_learning():
    # A. 구글 시트 저장 (실패 시 무시)
    try:
        sheet = get_sheet_object_for_write(st.session_state.mode_key)
        now = datetime.now().isoformat()
        learning_logs = []
        final_results = pd.DataFrame(st.session_state.analysis_result)
        
        if not final_results.empty:
            for _, row in final_results.iterrows():
                if not row.get('삭제', False):
                    learning_logs.append([
                        now, 
                        str(row.get('원본', '')).split('(')[0],
                        str(row.get('원형', '')), 
                        clean_val_for_save(row.get('분류', '')), 
                        "-", 
                        'add', 
                        'Engine-Final', '', ''
                    ])
        if learning_logs and sheet: 
            send_data_with_retry(sheet, learning_logs, True)
            fetch_all_rules_from_db.clear()
    except: pass

    # B. 엑셀 마스터 데이터 생성
    try:
        final_results = pd.DataFrame(st.session_state.analysis_result)
        
        # 데이터가 없어도 빈 프레임 생성
        if final_results.empty:
            temp_df = pd.DataFrame(columns=['구분', '자료', '출연횟수', '쪽수1'])
        else:
            valid = final_results[final_results['삭제']==False].copy()
            valid['n_cnt'] = valid['횟수'].apply(lambda x: int(re.sub(r'[^0-9]', '', str(x))) if re.search(r'\d', str(x)) else 1)
            agg = valid.groupby(['원형', '분류'], as_index=False).agg({'n_cnt': 'sum'})
            
            p_num = str(st.session_state.page_idx + st.session_state.start_offset)
            temp_rows = []
            
            for _, item in agg.iterrows():
                val = f"{p_num}_{item['n_cnt']}" if item['n_cnt'] > 1 else p_num
                temp_rows.append({
                    '구분': clean_val_for_save(item['분류']), 
                    '자료': item['원형'], 
                    '출연횟수': item['n_cnt'], 
                    '쪽수1': val
                })
            temp_df = pd.DataFrame(temp_rows)
            
        # 병합 및 저장
        st.session_state.master_df = merge_master_data(st.session_state.master_df, temp_df)
        st.toast("✅ 데이터 병합 완료!")
                
    except Exception as e:
        st.error(f"Save Logic Error: {str(e)}")

# [수정 2] 저장 로직 (함수 인식 오류 해결 + 데이터 무결성 보장)
def save_logic_with_learning():
    
    # [핵심] 도우미 함수 내장 (NameError 해결)
    def clean_func(v):
        try:
            if isinstance(v, str): 
                chars = ['🔵 ', '🟢 ', '🔴 ', '🟣 ', '📦 ', '🏃 ', '🎨 ', '⚡ ', '🔍 ', '👤 ', '❗ ']
                for c in chars: v = v.replace(c, '')
                return v.strip()
            return str(v)
        except: return ""

    # A. 구글 시트 저장 (실패 시 무시)
    try:
        sheet = get_sheet_object_for_write(st.session_state.mode_key)
        now = datetime.now().isoformat()
        learning_logs = []
        final_results = pd.DataFrame(st.session_state.analysis_result)
        
        if not final_results.empty:
            for _, row in final_results.iterrows():
                if not row.get('삭제', False):
                    learning_logs.append([
                        now, 
                        str(row.get('원본', '')).split('(')[0],
                        str(row.get('원형', '')), 
                        clean_func(row.get('분류', '')), 
                        "-", 
                        'add', 
                        'Engine-Final', '', ''
                    ])
        if learning_logs and sheet: 
            send_data_with_retry(sheet, learning_logs, True)
            fetch_all_rules_from_db.clear()
    except Exception as e:
        print(f"Google Sheet Save Error: {e}")

    # B. 엑셀 마스터 데이터 생성 (핵심)
    try:
        final_results = pd.DataFrame(st.session_state.analysis_result)
        
        # 분석 결과가 없어도 빈 데이터프레임이라도 만들어야 함
        if final_results.empty:
            temp_df = pd.DataFrame(columns=['구분', '자료', '출연횟수', '쪽수1'])
        else:
            valid = final_results[final_results['삭제']==False].copy()
            # 횟수 계산
            valid['n_cnt'] = valid['횟수'].apply(lambda x: int(re.sub(r'[^0-9]', '', str(x))) if re.search(r'\d', str(x)) else 1)
            
            # 합산
            agg = valid.groupby(['원형', '분류'], as_index=False).agg({'n_cnt': 'sum'})
            
            p_num = str(st.session_state.page_idx + st.session_state.start_offset)
            temp_rows = []
            
            for _, item in agg.iterrows():
                val = f"{p_num}_{item['n_cnt']}" if item['n_cnt'] > 1 else p_num
                temp_rows.append({
                    '구분': clean_func(item['분류']), 
                    '자료': item['원형'], 
                    '출연횟수': item['n_cnt'], 
                    '쪽수1': val
                })
            temp_df = pd.DataFrame(temp_rows)
            
        # 병합 실행 (여기서 master_df가 None이 되지 않게 갱신)
        st.session_state.master_df = merge_master_data(st.session_state.master_df, temp_df)
        
        # [중요] 저장 완료 후 상태값 변경은 여기서 하지 않음 (버튼 클릭 이벤트 내에서 처리)
        st.toast("✅ 데이터가 내부 저장소에 안전하게 기록되었습니다!")
                
    except Exception as e:
        st.error(f"Save Logic Error: {str(e)}")
        st.code(traceback.format_exc())

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

# [수정 1] 모델 선택 기능이 추가된 호출 함수
def api_call_direct(prompt, image_bytes=None, model_name=None):
    if not API_KEY: return None, "API Key Missing"
    
    # 모델 이름이 들어오면 그걸 쓰고(Flash), 없으면 기본값(Pro) 사용
    target_model = model_name if model_name else MODEL_NAME
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    
    parts = [{"text": prompt}]
    if image_bytes:
        optimized_img = process_image_for_api(image_bytes)
        if optimized_img:
            b64_img = base64.b64encode(optimized_img).decode('utf-8')
            parts.append({"inline_data": {"mime_type": "image/png", "data": b64_img}})

    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
    ]
            
    payload = {
        "contents": [{"parts": parts}],
        "safetySettings": safety_settings
    }
    
    for attempt in range(3):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=300)
            if res.status_code == 200:
                return res.json()['candidates'][0]['content']['parts'][0]['text'], "Success"
            elif res.status_code in [429, 500, 503]:
                time.sleep(2); continue
            else:
                return None, f"Error: {res.status_code} - {res.text}"
        except Exception as e: time.sleep(1)
            
    return None, "Error: 3회 재시도 실패"

# [수정 2] 텍스트 추출(OCR)은 저렴한 Flash로 처리
def extract_text_unified(file_bytes, file_type, page_idx):
    if not file_type: return ""
    raw_text = ""
    
    # 💰 비용 절감의 핵심: 읽기는 싸고 빠른 2.5 Flash가 담당
    ocr_model = "gemini-2.5-flash"
    
    if "image" in file_type: 
        raw_text, _ = api_call_direct("이 이미지 속의 텍스트를 모두 추출하세요. 줄바꿈 유지.", file_bytes, model_name=ocr_model)
    elif "pdf" in file_type:
        if FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
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
            raw_text, _ = api_call_direct("이 이미지 속의 텍스트를 모두 추출하세요. 줄바꿈 유지.", page_img, model_name=ocr_model)
        else:
            if PLUMBER_AVAILABLE:
                try:
                    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
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

# [수정 1] 품사(POS) 로직 완전 제거 (어종만 교정)
def apply_strict_rules(analysis_result, mode_key):
    db_rules = fetch_all_rules_from_db(mode_key)
    if not db_rules: return analysis_result

    # 족보 딕셔너리 생성
    rule_map = {}
    for row in db_rules:
        root = str(row.get('root_word', '')).strip()
        if root:
            rule_map[root] = {
                'origin': row.get('origin', ''),
                'action': row.get('action', '')
            }

    final_result = []
    # 어종 매핑 (이모지 포함)
    origin_map = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}

    for item in analysis_result:
        root = item.get('원형', '').strip()
        
        # 족보에 있는 단어라면?
        if root in rule_map:
            rule = rule_map[root]
            
            # '삭제' 규칙이면 결과에서 제외
            if rule['action'] == 'delete':
                continue 
            
            # '수정' 규칙이면 DB 내용으로 '어종'만 덮어씌움 (품사는 무시)
            if rule['origin']: 
                db_val = rule['origin'].replace("🔵 ", "").replace("🟢 ", "").replace("🔴 ", "").replace("🟣 ", "").strip()
                item['분류'] = origin_map.get(db_val, db_val)
                
        final_result.append(item)
        
    return final_result

# [수정 2] AI 분석 및 데이터 집계 (품사 제외 & 중복 폭발 방지)
def run_analysis_action(txt, img_bytes=None):
    if not txt.strip(): st.warning("내용이 없습니다."); return
    
    with st.spinner("AI가 어종(고유어/한자어/외래어)을 정밀 분석 중입니다..."):
        s_data = fetch_all_rules_from_db(st.session_state.mode_key)
        
        # [핵심] 품사 분석 요청을 삭제한 프롬프트
        prompt = f"""
        당신은 국어 어종 분석 전문가입니다. 
        텍스트에서 실질적인 의미를 가진 단어(명사, 용언의 어근)만 추출하여 어종을 분류하십시오.

        [분석 절대 규칙]
        1. **추출 대상**: 문맥상 의미가 있는 실질 형태소만 추출하십시오.
           - **제외 대상**: 조사, 어미, 의존명사(수/것/데/바/지/리...), 접사, 숫자, 특수기호.
        2. **원형**: 
           - 동사/형용사는 기본형(예: '먹다')으로, 명사는 조사를 뗀 형태로 적으십시오.
        3. **분류(어종)**:
           - 고유어(고), 한자어(한), 외래어(외), 혼종어(혼) 중 하나로 분류.
           - '하다' 동사(예: 공부하다)는 어근(공부)의 어종을 따름.
        
        {generate_prompt_from_sheet(s_data)}
        
        [출력 양식: JSON 리스트]
        [
          {{"원본": "텍스트그대로", "원형": "기본형", "분류": "고/한/외/혼"}}
        ]
        """
        
        # API 호출
        raw, status = api_call_direct(prompt + f"\n\n[분석 대상]:\n{txt[:5000]}", img_bytes)
        
        if raw: st.session_state.last_raw_response = raw
        else: st.session_state.last_raw_response = f"🚨 API 호출 실패! 이유: {status}"
        
        try:
            if not raw: raise Exception(f"API 응답 실패: {status}")
            
            clean_json = raw.replace("```json", "").replace("```", "").strip()
            match = re.search(r'\[.*\]', clean_json, re.DOTALL)
            if match: res = json.loads(match.group())
            else:
                try: res = json.loads(clean_json)
                except: res = []

            draft_items = []
            stop_words = ['것', '수', '데', '바', '지', '리', '개', '번', '명', '쪽', '등', '따름', '뿐', '이', '그', '저']

            for r in res:
                o = str(r.get('원본') or '').strip()
                root = str(r.get('원형') or '').strip()
                orig_v = str(r.get('분류') or '혼').strip()
                
                if not o or not root: continue
                if re.search(r'[0-9a-zA-Z]', o) or re.search(r'[0-9a-zA-Z]', root): continue
                if root in stop_words: continue
                
                draft_items.append({'원본': o, '원형': root, '분류': orig_v})

            # DB 족보 적용
            draft_items = apply_strict_rules(draft_items, st.session_state.mode_key)
            st.session_state.initial_draft = draft_items

            # [핵심] 품사 없이 (원형, 분류)로만 그룹핑 -> 6만개 중복이 1개로 합쳐짐!
            proc = []
            temp_dict = {}
            for item in draft_items:
                key = (item['원형'], item['분류']) 
                if key not in temp_dict: temp_dict[key] = []
                temp_dict[key].append(item['원본'])

            for (root, origin), origs in temp_dict.items():
                cnts = Counter(origs)
                display_orig = ", ".join([f"{w}({c})" for w, c in cnts.items()])
                total_cnt = sum(cnts.values())
                
                final_origin = origin
                if not any(x in origin for x in ['🔵','🟢','🔴','🟣']):
                    origin_lower = origin.lower()
                    if '고' in origin_lower: final_origin = '🔵 고'
                    elif '한' in origin_lower: final_origin = '🟢 한'
                    elif '외' in origin_lower: final_origin = '🔴 외'
                    else: final_origin = '🟣 혼'

                proc.append({
                    "삭제": False, 
                    "횟수": f"{total_cnt}회", 
                    "원본": display_orig, 
                    "원형": root, 
                    "분류": final_origin
                    # 품사 필드 삭제됨
                })
            
            # 메모리 청소
            if 'file_bytes' in st.session_state: del st.session_state.file_bytes
            
            st.session_state.analysis_result = proc; st.session_state.step = 3; st.rerun()
            
        except Exception as e:
            st.error(f"파싱 오류: {str(e)}")
            st.session_state.debug_log = f"Error: {str(e)}\nRaw Response Logged."

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
        # [추가] 내 계정에서 사용 가능한 모델 목록 확인하기
    if st.button("📋 사용 가능한 모델 목록 보기"):
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"
        r = requests.get(url); st.code(r.text) # 화면에 JSON으로 쫙 보여줍니다


# STEP 0: 언어 규범 선택
if st.session_state.step == 0:
    st.markdown("<h1 style='text-align: center; margin-bottom: 20px;'>📚 국어활동 AI 분석기</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #888; margin-bottom: 50px;'>원하는 언어 규범을 선택하여 분석을 시작하세요.</p>", unsafe_allow_html=True)
    
    c_left, c_south, c_north, c_right = st.columns([1, 4, 4, 1])
    with c_south:
        if st.button("🏛️\n\n대한민국 표준어\n\n(표준국어대사전 기준)", use_container_width=True):
            reset_input_buffer()
            st.session_state.mode_key = "SOUTH"; st.session_state.step = 1; st.rerun()
    with c_north:
        if st.button("🏔️\n\n북한 문화어\n\n(문화어 규범 기준)", use_container_width=True):
            reset_input_buffer()
            st.session_state.mode_key = "NORTH"; st.session_state.step = 1; st.rerun()

# STEP 1: 데이터 소스 선택
elif st.session_state.step == 1:
    c1, c2 = st.columns([8, 2])
    with c1: st.header("📂 데이터 소스 선택")
    with c2: 
        if st.button("⬅️ 모드 선택으로", use_container_width=True): 
            reset_input_buffer()
            st.session_state.step = 0; st.rerun()

    col1, col2 = st.columns(2)
    # 기존: elif st.session_state.step == 1: ... 내부의 엑셀 업로드 부분 찾아서 교체
    with col1:
        with st.container(border=True):
            st.subheader("📂 이어하기")
            st.caption("쪽수 정보가 흩어진 파일도 자동으로 하나로 합쳐서 복구합니다.")
            up_excel = st.file_uploader("기존 분석 엑셀 업로드", type=['xlsx'])
            if up_excel:
                try:
                    df = pd.read_excel(up_excel, engine='openpyxl')
                    
                    # [수정] 복구 로직 강화 (6만개 행 압축)
                    if '자료' in df.columns and '구분' in df.columns:
                        st.toast("파일 최적화 중...")
                        page_cols = [c for c in df.columns if str(c).startswith('쪽수')]
                        
                        # 데이터를 녹여서(Melt) 한 줄로 만듦
                        melted = df.melt(id_vars=['자료', '구분'], value_vars=page_cols, value_name='page')
                        melted = melted.dropna(subset=['page'])
                        
                        # 같은 단어끼리 묶어서 페이지 번호를 리스트로 합침
                        grouped = melted.groupby(['자료', '구분'])['page'].apply(
                            lambda x: sorted(list(set([str(v).strip() for v in x if str(v).strip() not in ['nan', '']])))
                        ).reset_index(name='merged_pages')
                        
                        # 다시 엑셀 형태로 펼침
                        new_rows = []
                        for _, row in grouped.iterrows():
                            pages = row['merged_pages']
                            base_data = {
                                '자료': row['자료'], 
                                '구분': row['구분'],
                                '출연횟수': len(pages)
                            }
                            for i, p in enumerate(pages):
                                base_data[f'쪽수{i+1}'] = p
                            new_rows.append(base_data)
                            
                        st.session_state.master_df = pd.DataFrame(new_rows)
                        st.success(f"복구 완료! {len(df)}행 -> {len(new_rows)}행으로 최적화됨.")
                        st.session_state.step = 1.5
                        st.rerun()
                    else:
                        st.error("형식이 올바르지 않습니다.")
                except Exception as e:
                    st.error(f"오류: {str(e)}")
    with col2:
        with st.container(border=True):
            st.subheader("🆕 새로 시작하기")
            if st.button("새 프로젝트 생성", use_container_width=True): st.session_state.master_df = None; st.session_state.step = 1.5; st.rerun()

# STEP 1.5: 입력 방식 선택
elif st.session_state.step == 1.5:
    c1, c2 = st.columns([8, 2])
    with c1: st.header("📝 입력 방식 선택")
    with c2: 
        if st.button("⬅️ 소스 선택으로", use_container_width=True): 
            reset_input_buffer()
            st.session_state.step = 1; st.rerun()

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("📄\n\nPDF 문서 분석\n\n(쪽수 관리 지원)", use_container_width=True):
            reset_input_buffer()
            st.session_state.input_type = "PDF"; st.session_state.step = 2; st.rerun()
    with c2:
        if st.button("🖼️\n\n이미지 분석\n\n(단일 사진 전용)", use_container_width=True):
            reset_input_buffer()
            st.session_state.input_type = "IMAGE"; st.session_state.step = 2; st.rerun()
    with c3:
        if st.button("✍️\n\n텍스트 직접 입력\n\n(복사한 글 분석)", use_container_width=True):
            reset_input_buffer()
            st.session_state.input_type = "DIRECT"; st.session_state.step = 2; st.rerun()

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
        # 에러가 났을 때 입력 화면(Step 2)에서도 로그를 보여주는 코드
        
if st.session_state.debug_mode:
        st.markdown("---")
        st.markdown("### 🐞 디버그 로그 (관리자용 - 입력 화면)")
        
        # 1. 파이썬 내부 에러 로그
        if st.session_state.debug_log:
            st.error("💥 상세 에러 내용:")
            st.code(st.session_state.debug_log, language="text")
        else:
            st.info("기록된 에러 로그가 없습니다.")
            
        # 2. 구글 AI가 보낸 원본 메시지 (이걸 봐야 거절 사유를 알 수 있음)
        if st.session_state.last_raw_response:
            st.warning("🤖 AI가 보낸 원본 응답 (Raw Response):")
            st.code(st.session_state.last_raw_response, language="json")

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
    # 기존: elif st.session_state.step == 3: ... 내부
    st.markdown("### 📋 분석 결과 편집")
    
    df_res = pd.DataFrame(st.session_state.analysis_result)
    
    if not df_res.empty:
        # [수정] 품사 선택 설정 삭제
        edited = st.data_editor(
            df_res, 
            column_config={
                "삭제": st.column_config.CheckboxColumn("삭제"),
                "횟수": st.column_config.TextColumn("횟수", disabled=True),
                "원본": st.column_config.TextColumn("원본", disabled=True),
                "원형": st.column_config.TextColumn("원형"), 
                "분류": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"])
            }, 
            use_container_width=True, 
            num_rows="dynamic", 
            key="editor_final"
        )
        
        # 동기화
        st.session_state.analysis_result = edited.to_dict('records')
        
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
        # 버튼 3개 배치 (추가 / 삭제 / 저장)
        b1, b2, b3 = st.columns([1, 1, 2])
        
        with b1: 
            if st.button("➕ 단어 추가", use_container_width=True): open_add_dialog()
            
        with b2:
            if st.button("⛔ 선택 삭제", use_container_width=True):
                st.session_state.analysis_result = [r for r in st.session_state.analysis_result if not r.get('삭제', False)]
                st.toast("🗑️ 삭제 완료")
                time.sleep(1.0)
                st.rerun()
                
        with b3:
            # [저장 버튼] 클릭 시 저장하고 -> 상태 변경(is_finished=True) -> 리런
            if st.button("💾 이 페이지 결과 저장", type="primary", use_container_width=True):
                save_logic_with_learning()
                st.session_state.is_finished = True
                st.rerun() # 즉시 새로고침하여 아래 'else' 블록을 보여줌

    else:
        # 저장 완료 상태: [성공 메시지] + [엑셀 다운로드] + [다음 쪽 이동]
        st.success("✅ 저장이 완료되었습니다! 엑셀을 다운로드하거나 다음 쪽으로 이동하세요.")
        
        # 엑셀 생성 (빈 데이터 방어 포함)
        buf = io.BytesIO()
        try:
            with pd.ExcelWriter(buf, engine='openpyxl') as w: 
                if st.session_state.master_df is not None and not st.session_state.master_df.empty:
                    st.session_state.master_df.to_excel(w, index=False)
                else:
                    pd.DataFrame(columns=['구분','자료','출연횟수']).to_excel(w, index=False)
        except Exception as e:
            st.error(f"엑셀 생성 오류: {e}")

        c_down, c_next = st.columns([1, 1])
        
        with c_down:
            st.download_button(
                label="📥 엑셀 다운로드", 
                data=buf.getvalue(), 
                file_name=f"분석결과_{datetime.now().strftime('%m%d_%H%M')}.xlsx", 
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 
                use_container_width=True, 
                type="primary"
            )
        
        # PDF일 때만 다음 쪽 이동 버튼 표시
        if st.session_state.input_type == "PDF":
            with c_next:
                # 마지막 페이지가 아닐 때만 활성화
                if st.session_state.page_idx < st.session_state.total_pages - 1:
                    if st.button("➡️ 다음 쪽으로 이동", use_container_width=True):
                        st.session_state.page_idx += 1
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, "application/pdf", st.session_state.page_idx)
                        st.session_state.analysis_result = []
                        st.session_state.step = 2
                        st.session_state.is_finished = False
                        st.rerun()
                else:
                    st.info("마지막 페이지입니다.")