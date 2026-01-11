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
from datetime import datetime
from collections import Counter

# =========================================================
# [0] 라이브러리 임포트 및 상태 체크 (GP9 원본 안전장치)
# =========================================================
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

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# 페이지 기본 설정
st.set_page_config(
    page_title="국어활동 AI 분석기 (Ultimate Fixed)", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# =========================================================
# [1] API 및 스타일 설정 (눈 보호 모드 적용)
# =========================================================
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = ""

MODEL_NAME = "gemini-2.0-flash-exp"
SHEET_NAME = "Korean_DB"
TRUST_THRESHOLD = 3 

# [CSS] 텍스트창 진한 회색(#262730) 배경 + 흰색 글씨 (눈 보호 모드)
st.markdown("""
    <style>
        .stTextArea textarea { 
            font-size: 16px !important; 
            line-height: 1.6 !important; 
            font-family: 'Malgun Gothic', sans-serif !important; 
            background-color: #262730 !important; 
            color: #ffffff !important; 
            border: 1px solid #4a4a4a !important; 
            font-weight: 400 !important;
        }
        .stTextArea textarea:focus {
            border: 1px solid #ff4b4b !important;
        }
        .stDataFrame { border: 1px solid #ddd; }
        .block-container { padding-top: 2rem; }
        div[data-testid="stExpander"] details summary p {
            font-size: 1.05rem;
            font-weight: 600;
        }
        div.stButton > button {
            font-weight: bold;
        }
    </style>
""", unsafe_allow_html=True)

# =========================================================
# [2] 구글 시트 & 백업 시스템 (GP9 원본 로직 완벽 복구)
# =========================================================
@st.cache_resource
def get_google_sheet_client():
    if not GSPREAD_AVAILABLE: return None
    try:
        if "gcp_service_account" not in st.secrets: return None
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
             creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        return client
    except: return None

def get_sheet_data_fresh(mode_key):
    client = get_google_sheet_client()
    if not client: return None, []
    target_sheet_name = "South_Korea" if mode_key == "SOUTH" else "North_Korea"
    try:
        spreadsheet = client.open(SHEET_NAME)
        try:
            sheet = spreadsheet.worksheet(target_sheet_name)
        except gspread.WorksheetNotFound:
            st.warning(f"⚠️ '{target_sheet_name}' 시트가 없어 새로 생성합니다.")
            sheet = spreadsheet.add_worksheet(title=target_sheet_name, rows=1000, cols=20)
            sheet.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context"])
        data = sheet.get_all_records()
        return sheet, data
    except: return None, []

def send_data_with_retry(sheet_obj, data, is_multiple=False):
    if not sheet_obj: return False
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if is_multiple:
                clean_data = [[str(item) for item in row] for row in data]
                sheet_obj.append_rows(clean_data)
            else:
                clean_data = [str(item) for item in data]
                sheet_obj.append_row(clean_data)
            return True
        except:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return False
    return False

def save_backup_to_cloud(mode_key, df):
    client = get_google_sheet_client()
    if not client or df is None or df.empty: return False
    backup_sheet_name = f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}"
    try:
        spreadsheet = client.open(SHEET_NAME)
        try:
            worksheet = spreadsheet.worksheet(backup_sheet_name)
            worksheet.clear()
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=backup_sheet_name, rows=1000, cols=20)
        
        df_str = df.fillna("").astype(str)
        data_to_upload = [df_str.columns.values.tolist()] + df_str.values.tolist()
        worksheet.update(data_to_upload)
        return True
    except Exception as e:
        print(f"백업 실패: {e}") 
        return False

def load_backup_from_cloud(mode_key):
    client = get_google_sheet_client()
    if not client: return None
    backup_sheet_name = f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}"
    try:
        spreadsheet = client.open(SHEET_NAME)
        worksheet = spreadsheet.worksheet(backup_sheet_name)
        data = worksheet.get_all_records()
        return pd.DataFrame(data) if data else None
    except: return None

# =========================================================
# [3] AI 엔진 (1~7단계 + GP9 OCR)
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    for row in sheet_data[-50:]:
        if row.get('action') == 'delete':
            rules.append(f"- [삭제 규칙]: '{row.get('original_word')}' 제외")
        elif row.get('action') in ['add', 'modify']:
            rules.append(f"- [고정 규칙]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}', 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    if rules:
        return "\n[🚨 최우선 사용자 학습 규칙]:\n" + "\n".join(rules) + "\n"
    return ""

def api_call_direct(prompt, image_bytes=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    parts = [{"text": prompt}]
    
    if image_bytes:
        b64_image = base64.b64encode(image_bytes).decode('utf-8')
        parts.append({"inline_data": {"mime_type": "image/png", "data": b64_image}})
    
    data = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1, "maxOutputTokens": 8192
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code != 200: return None
        result_json = response.json()
        if 'candidates' in result_json:
            return result_json['candidates'][0]['content']['parts'][0]['text']
        return None
    except: return None

def api_call_vision_ocr(image_bytes):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    prompt_text = "이 이미지에 있는 텍스트를 보이는 그대로 추출해주세요. 말풍선, 단락은 줄바꿈으로 구분."
    data = {"contents": [{"parts": [{"text": prompt_text}, {"inline_data": {"mime_type": "image/png", "data": base64_image}}]}], "generationConfig": {"temperature": 0.1}}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        return ""
    except: return ""

def split_text_smartly(text, chunk_size=1000):
    sentences = re.split(r'(?<=[.?!])\s+|\\n', text)
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) < chunk_size:
            current_chunk += sentence + " "
        else:
            if current_chunk: chunks.append(current_chunk.strip())
            current_chunk = sentence + " "
    if current_chunk: chunks.append(current_chunk.strip())
    return chunks

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    mode_desc = "대한민국 표준어" if mode_key == "SOUTH" else "북한 문화어(두음법칙 미적용)"
    
    prompt = f"""
    당신은 '{mode_desc}' 형태소 분석 전문가입니다.
    {learning_prompt}
    
    [분석 단계 (Chain of Thought)]
    1. **문맥 파악**: '{mode_desc}' 규칙 적용.
    2. **형태소 분리**: 조사, 어미 제거.
    3. **'하다' 용언 처리 (중요)**: '명사+하다'는 문맥에 따라 동사/명사로 판단.
    4. **품사 필터링**: 명사, 동사, 형용사, 부사, 관형사, 대명사만 남김.
    5. **출력**: JSON 포맷.

    [JSON 예시]
    [
        {{"original_word": "배를", "root_word": "배", "origin": "고", "pos": "명사"}},
        {{"original_word": "공부했다", "root_word": "공부", "origin": "한", "pos": "동사"}}
    ]
    """
    
    if image_bytes:
        full_prompt = f"{prompt}\n\n(이미지 OCR 결과 참고)"
        res_text = api_call_direct(full_prompt, image_bytes)
        if res_text:
            try:
                match = re.search(r'\[.*\]', res_text, re.DOTALL)
                if match: return json.loads(match.group())
                s = res_text.find('[')
                e = res_text.rfind(']') + 1
                if s != -1 and e != -1: return json.loads(res_text[s:e])
            except: return []
        return []
    else:
        chunks = split_text_smartly(text)
        all_results = []
        for chunk in chunks:
            full_prompt = f"{prompt}\n\n[분석할 텍스트]:\n{chunk}"
            res_text = api_call_direct(full_prompt)
            if res_text:
                try:
                    match = re.search(r'\[.*\]', res_text, re.DOTALL)
                    if match: all_results.extend(json.loads(match.group()))
                    else:
                        s = res_text.find('[')
                        e = res_text.rfind(']') + 1
                        if s != -1 and e != -1: all_results.extend(json.loads(res_text[s:e]))
                except: pass
            time.sleep(0.1) 
        return all_results

# =========================================================
# [4] 파일 처리 및 텍스트 추출 (GP9 로직 + seek(0) 수정)
# =========================================================
def extract_text_unified(file_obj, page_idx):
    """GP9의 정밀 추출 로직 (Crop -> OCR Fallback) + seek(0) 수정"""
    file_type = file_obj.type
    
    # [핵심 수정] 파일 포인터 초기화 (이게 없어서 추출이 안 됐음)
    file_obj.seek(0)
    
    if "image" in file_type:
        try: return api_call_vision_ocr(file_obj.getvalue())
        except: return ""
        
    elif "pdf" in file_type:
        text = ""
        # 1. pdfplumber 시도 (GP9의 영역 Crop 기능 포함)
        if PLUMBER_AVAILABLE:
            try:
                # Plumber 사용 전에도 seek(0)
                file_obj.seek(0)
                with pdfplumber.open(file_obj) as pdf:
                    if page_idx < len(pdf.pages):
                        page = pdf.pages[page_idx]
                        width, height = page.width, page.height
                        try:
                            # 상단 5%, 하단 10% 제외하고 크롭 (GP9 로직)
                            crop_box = (0, height * 0.05, width, height * 0.9)
                            cropped = page.crop(crop_box)
                            text = cropped.extract_text()
                        except:
                            text = page.extract_text()
            except: pass
        
        # 2. Fitz 시도 (Plumber 실패 시)
        if not text and FITZ_AVAILABLE:
            try:
                # Fitz 사용 전에도 seek(0)
                file_obj.seek(0)
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_idx < len(doc):
                    text = doc[page_idx].get_text()
            except: pass
        
        # 3. [핵심 복구] 텍스트가 여전히 없으면 Vision OCR (스캔본 대응)
        # 텍스트가 30자 미만이면 이미지로 간주
        if (not text or len(text.strip()) < 30) and FITZ_AVAILABLE:
            try:
                file_obj.seek(0) # [핵심 수정]
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_idx < len(doc):
                    # 해상도 높여서 이미지 변환
                    pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2, 2))
                    img_bytes = pix.tobytes("png")
                    return api_call_vision_ocr(img_bytes)
            except: pass
            
        return text if text else ""
    return ""

def get_page_image_bytes(file_obj, page_idx):
    """뷰어용 이미지 생성"""
    file_type = file_obj.type
    
    # [핵심 수정] 뷰어 생성 시에도 seek(0)
    file_obj.seek(0)
    
    if "image" in file_type:
        return file_obj.getvalue()
    elif "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
            if 0 <= page_idx < len(doc):
                pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0)) 
                return pix.tobytes("png")
        except: pass
    return None

# =========================================================
# [5] 데이터 저장 로직 (GP9 동적 컬럼)
# =========================================================
def clean_val(v):
    if isinstance(v, str):
        return v.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('👤 ', '')
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

def save_logic(edited_df, page_str, sheet_obj, context_text):
    if sheet_obj:
        logs = []
        for _, row in edited_df.iterrows():
            if not row['delete_check']:
                logs.append([
                    datetime.now().isoformat(), row['original_word'], row['root_word'], 
                    clean_val(row['origin']), clean_val(row['pos']), 'modify', context_text[:50]
                ])
        if logs: send_data_with_retry(sheet_obj, logs, is_multiple=True)

    valid_rows = edited_df[edited_df['delete_check'] == False].copy()
    def parse_count(val):
        try: return int(str(val).replace('회', '').strip())
        except: return 1
    valid_rows['numeric_count'] = valid_rows['count'].apply(parse_count)
    
    aggregated = valid_rows.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({
        'numeric_count': 'sum', 'original_word': lambda x: ', '.join(x.unique())
    })
    
    if st.session_state.master_df is None:
        st.session_state.master_df = pd.DataFrame(columns=['구분', '자료', '출연횟수', '쪽수1'])
    master = st.session_state.master_df
    
    for c in master.columns:
        if '쪽수' in c: master[c] = master[c].astype(object)

    new_rows_list = []
    for _, item in aggregated.iterrows():
        root = item['root_word']
        origin_val = clean_val(item['origin'])
        cnt = item['numeric_count']
        val_to_save = f"{page_str}_{cnt}" if cnt > 1 else page_str
        
        mask = (master['자료'] == root) & (master['구분'] == origin_val)
        if mask.any():
            idx = master[mask].index[0]
            filled_cols = [c for c in master.columns if '쪽수' in c and pd.notna(master.at[idx, c])]
            next_col = f"쪽수{len(filled_cols) + 1}"
            if next_col not in master.columns: master[next_col] = None 
            master.at[idx, next_col] = val_to_save
        else:
            new_rows_list.append({
                '구분': origin_val, '자료': root, '출연횟수': 0, '쪽수1': val_to_save
            })
            
    if new_rows_list:
        master = pd.concat([master, pd.DataFrame(new_rows_list)], ignore_index=True)
    
    master['출연횟수'] = master.apply(calc_freq, axis=1)
    
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    master['sort_key'] = master['구분'].map(sort_map).fillna(5)
    master = master.sort_values(['sort_key', '자료']).drop('sort_key', axis=1)
    st.session_state.master_df = master
    
    save_backup_to_cloud(st.session_state.last_mode, master)
    return True

# =========================================================
# [6] 메인 UI 구성
# =========================================================
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'file_hash' not in st.session_state: st.session_state.file_hash = None
if 'start_page_offset' not in st.session_state: st.session_state.start_page_offset = 1
if 'manual_page_input' not in st.session_state: st.session_state.manual_page_input = "1"
if 'last_uploaded_file_name' not in st.session_state: st.session_state.last_uploaded_file_name = None
if 'editor_text_content' not in st.session_state: st.session_state.editor_text_content = ""

st.title("📝 국어활동 AI 분석기")

with st.sidebar:
    st.header("⚙️ 설정")
    mode = st.radio("언어 모드", ["🇰🇷 표준어", "🇰🇵 문화어"])
    MODE_KEY = "SOUTH" if "표준어" in mode else "NORTH"
    
    if 'last_mode' not in st.session_state: st.session_state.last_mode = MODE_KEY
    if st.session_state.last_mode != MODE_KEY:
        if st.session_state.master_df is not None:
            save_backup_to_cloud(st.session_state.last_mode, st.session_state.master_df)
        st.session_state.master_df = None
        st.session_state.last_mode = MODE_KEY
        st.rerun()
        
    sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)
    if sheet: st.caption(f"✅ 학습 데이터: {len(sheet_data)}건")
    else: st.error("❌ 연결 실패")
    
    st.markdown("---")
    st.header("📂 이어하기")
    up_excel = st.file_uploader("엑셀 파일 선택", type=['xlsx'])
    
    if up_excel and up_excel.name != st.session_state.last_uploaded_file_name:
        if st.button("병합하기"):
            try:
                loaded = pd.read_excel(up_excel)
                if st.session_state.master_df is not None:
                    cols = ['자료', '구분']
                    m = pd.concat([st.session_state.master_df, loaded]).drop_duplicates(subset=cols, keep='first')
                    st.session_state.master_df = m
                else:
                    st.session_state.master_df = loaded
                st.session_state.last_uploaded_file_name = up_excel.name
                st.success("완료!")
                time.sleep(1)
                st.rerun()
            except: st.error("오류")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("☁️ 백업"): 
            if save_backup_to_cloud(MODE_KEY, st.session_state.master_df): st.toast("성공")
    with c2:
        if st.button("🔄 복구"):
            r = load_backup_from_cloud(MODE_KEY)
            if r is not None: 
                st.session_state.master_df = r
                st.toast("복구 성공"); time.sleep(1); st.rerun()
            else: st.warning("없음")

    st.markdown("---")
    with st.expander("➕ 수동 추가"):
        with st.form("manual"):
            o = st.text_input("원본"); r = st.text_input("원형")
            org = st.selectbox("분류", ["고","한","외","혼"]); p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사"])
            if st.form_submit_button("추가"):
                send_data_with_retry(sheet, [datetime.now().isoformat(), o, r, org, p, 'add', '수동'])
                st.toast("추가됨")

    st.markdown("---")
    st.caption("🔍 이력 검색")
    q = st.text_input("검색", placeholder="단어")
    if q and sheet_data:
        f = [row for row in sheet_data if q in str(row.get('root_word')) or q in str(row.get('original_word'))]
        for item in f[-3:]: st.text(f"[{item.get('action')}] {item.get('root_word')}")

st.subheader("1. 분석 자료 입력")
main_file = st.file_uploader("PDF/이미지 파일", type=['pdf', 'png', 'jpg'])

# [오류 해결] AttributeError 방지 (id 대신 name+size)
if main_file:
    fid = f"{main_file.name}_{main_file.size}"
    if st.session_state.file_hash != fid:
        st.session_state.file_hash = fid
        st.session_state.page_idx = 0
        st.session_state.analysis_result = []
        # 파일이 변경되면 즉시 텍스트 추출 실행
        st.session_state.editor_text_content = extract_text_unified(main_file, 0)
        st.rerun()

total_pages = 1
if main_file and "pdf" in main_file.type:
    try:
        if PLUMBER_AVAILABLE:
            with pdfplumber.open(main_file) as pdf: total_pages = len(pdf.pages)
        elif FITZ_AVAILABLE:
            doc = fitz.open(stream=main_file.getvalue(), filetype="pdf")
            total_pages = len(doc)
    except: pass

col_v, col_i = st.columns([1, 1])

with col_v:
    if main_file:
        st.info("📷 미리보기")
        img = get_page_image_bytes(main_file, st.session_state.page_idx)
        if img: st.image(img, use_container_width=True)
        else: st.warning("표시 불가")
        
        if "pdf" in main_file.type:
            c1, c2, c3 = st.columns([1, 2, 1])
            with c1:
                if st.button("◀"):
                    st.session_state.page_idx = max(0, st.session_state.page_idx - 1)
                    st.session_state.editor_text_content = extract_text_unified(main_file, st.session_state.page_idx)
                    st.rerun()
            with c3:
                if st.button("▶"):
                    st.session_state.page_idx = min(total_pages - 1, st.session_state.page_idx + 1)
                    st.session_state.editor_text_content = extract_text_unified(main_file, st.session_state.page_idx)
                    st.rerun()
            with c2:
                target = st.number_input("이동", 1, total_pages, st.session_state.page_idx+1)
                if target-1 != st.session_state.page_idx:
                    st.session_state.page_idx = target-1
                    st.session_state.editor_text_content = extract_text_unified(main_file, st.session_state.page_idx)
                    st.rerun()
            
            st.session_state.start_page_offset = st.number_input("시작 쪽수", value=st.session_state.start_page_offset)
            page_str = str(st.session_state.page_idx + st.session_state.start_page_offset)
        else:
            page_str = st.text_input("쪽수", value="1")
    else:
        st.info("파일 없음")
        page_str = st.text_input("쪽수", value="1")

with col_i:
    st.info("📝 분석 내용 (수정 가능)")
    txt_val = st.text_area("텍스트", value=st.session_state.editor_text_content, height=500, key="editor_area")
    if txt_val != st.session_state.editor_text_content:
        st.session_state.editor_text_content = txt_val

    if st.button("🚀 분석 실행", type="primary", use_container_width=True):
        if not txt_val.strip(): st.warning("내용 없음")
        else:
            with st.spinner("분석 중..."):
                s_img = img if (main_file and len(txt_val)<30) else None
                res = get_analysis_hybrid(txt_val, s_img, sheet_data, MODE_KEY)
                
                proc = []
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                
                # [오류 해결] KeyError 방지용 .get()
                cnts = Counter([r.get('original_word', '미상') for r in res])
                seen = set()
                for r in res:
                    root = r.get('root_word', '')
                    ro = r.get('origin', '혼')
                    rp = r.get('pos', '명사')
                    row = r.get('original_word', '미상')
                    if root not in seen:
                        proc.append({
                            "delete_check": False,
                            "count": f"{cnts[row]}회",
                            "original_word": row,
                            "root_word": root,
                            "origin": om.get(ro, ro),
                            "pos": pm.get(rp, rp)
                        })
                        seen.add(root)
                st.session_state.analysis_result = proc

if st.session_state.analysis_result:
    st.divider()
    st.subheader("2. 결과 확인")
    df_r = pd.DataFrame(st.session_state.analysis_result)
    edited = st.data_editor(
        df_r,
        column_config={
            "delete_check": st.column_config.CheckboxColumn("삭제"),
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사"])
        },
        num_rows="dynamic",
        use_container_width=True
    )
    
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("⛔ 삭제"):
            dels = edited[edited['delete_check']==True]
            if not dels.empty and sheet:
                l = [[datetime.now().isoformat(), r['original_word'], r['root_word'], "", "", 'delete', 'User'] for _, r in dels.iterrows()]
                send_data_with_retry(sheet, l, True)
                st.session_state.analysis_result = edited[edited['delete_check']==False].to_dict('records')
                st.rerun()
    with c2:
        if st.button("💾 저장+이동 (▶)"):
            if save_logic(edited, page_str, sheet, txt_val):
                st.toast("저장됨")
                if main_file and "pdf" in main_file.type and st.session_state.page_idx < total_pages-1:
                    st.session_state.page_idx += 1
                    st.session_state.editor_text_content = extract_text_unified(main_file, st.session_state.page_idx)
                    st.session_state.analysis_result = []
                    time.sleep(0.5); st.rerun()
    with c3:
        if st.button("💾 저장만"):
            if save_logic(edited, page_str, sheet, txt_val): st.success("저장됨")

if st.session_state.master_df is not None:
    st.markdown("---")
    st.subheader("📊 전체 데이터")
    st.dataframe(st.session_state.master_df, use_container_width=True)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w: st.session_state.master_df.to_excel(w, index=False)
    st.download_button("📥 엑셀 다운로드", buf.getvalue(), "final.xlsx")