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
# [0] 라이브러리 임포트 및 상태 체크
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
    page_title="국어활동 AI 분석기 (Real Complete)", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# =========================================================
# [1] API 및 스타일 설정
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

st.markdown("""
    <style>
        .stTextArea textarea { 
            font-size: 16px !important; 
            line-height: 1.6 !important; 
            font-family: 'Malgun Gothic', sans-serif !important; 
            background-color: #262730 !important; 
            color: #ffffff !important; 
            border: 1px solid #555 !important; 
            font-weight: 400 !important;
        }
        .stTextArea textarea:focus {
            border: 1px solid #ff4b4b !important;
        }
        .stDataFrame { border: 1px solid #444; }
        .block-container { padding-top: 2rem; }
        
        .debug-box {
            background-color: #222;
            color: #0f0;
            padding: 8px;
            margin: 2px 0;
            border-radius: 4px;
            font-family: monospace;
            font-size: 0.8rem;
        }
        .debug-success { color: #4caf50; }
        .debug-err { color: #ff5252; }
        .debug-warn { color: #fb8c00; }
    </style>
""", unsafe_allow_html=True)

if 'debug_logs' not in st.session_state: st.session_state.debug_logs = []

def log_debug(msg, type="info"):
    if st.session_state.get('debug_mode'):
        color_class = f"debug-{type}"
        st.session_state.debug_logs.append(f"<div class='debug-box {color_class}'>[{datetime.now().strftime('%H:%M:%S')}] {msg}</div>")

# =========================================================
# [2] 구글 시트 & 백업 시스템
# =========================================================
@st.cache_resource
def get_google_sheet_client():
    if not GSPREAD_AVAILABLE: return None
    try:
        if "gcp_service_account" not in st.secrets: return None
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(dict(st.secrets["gcp_service_account"]), scope)
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

def load_backup_from_cloud(mode_key):
    client = get_google_sheet_client()
    if not client: return None
    try:
        sh = client.open(SHEET_NAME)
        ws = sh.worksheet(f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}")
        data = ws.get_all_records()
        return pd.DataFrame(data) if data else None
    except: return None

# =========================================================
# [3] AI 엔진 (기능 보존: 문맥 분석 + 스마트 분할 + 동음이의어/인명/지명 구분)
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    for row in sheet_data[-50:]:
        if row.get('action') == 'delete': rules.append(f"- [삭제 규칙]: '{row.get('original_word')}'는 분석 결과에서 제외하세요.")
        elif row.get('action') in ['add', 'modify']: rules.append(f"- [고정 규칙]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}', 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    return "\n[사용자 학습 규칙 (최우선 적용)]:\n" + "\n".join(rules) + "\n" if rules else ""

def api_call_direct(prompt, image_bytes=None):
    if not API_KEY: 
        log_debug("API Key 누락", "err")
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    parts = [{"text": prompt}]
    if image_bytes:
        parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_bytes).decode('utf-8')}})
    
    try:
        res = requests.post(url, headers=headers, json={"contents": [{"parts": parts}]}, timeout=300)
        if res.status_code == 200: return res.json()['candidates'][0]['content']['parts'][0]['text']
        else: log_debug(f"API Error: {res.text}", "err"); return None
    except Exception as e: log_debug(f"Conn Error: {e}", "err"); return None

def api_call_vision_ocr(image_bytes):
    log_debug("Vision OCR 요청", "info")
    res = api_call_direct("이미지 내 텍스트를 있는 그대로 추출해줘. 줄바꿈 유지.", image_bytes)
    if res:
        log_debug(f"OCR 성공 ({len(res)}자)", "success")
        return res
    log_debug("OCR 응답 없음", "err")
    return ""

def split_text_smartly(text, chunk_size=1000):
    """[복구] 긴 문장 안전 분할"""
    sentences = re.split(r'(?<=[.?!])\s+|\\n', text)
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) < chunk_size:
            current_chunk += sentence + " "
        else:
            chunks.append(current_chunk.strip())
            current_chunk = sentence + " "
    if current_chunk: chunks.append(current_chunk.strip())
    return chunks

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    # [핵심] 동음이의어(괄호) 및 인명/지명(고유명사) 구분 프롬프트 추가
    prompt = f"""
    당신은 '{"대한민국 표준어" if mode_key=="SOUTH" else "북한 문화어"}' 형태소 분석 전문가입니다.
    {generate_prompt_from_sheet(sheet_data)}
    
    [분석 단계 (Chain of Thought)]
    1. **문맥 파악**: '{mode_key}' 규칙 적용.
    2. **형태소 분리**: 조사(은/는/이/가 등)와 어미 제거.
    3. **'하다' 용언 처리**: 문맥에 따라 동사/명사 판단.
    4. **품사 필터링**: 명사, 동사, 형용사, 부사, 관형사, 대명사만 남김.
    5. **동음이의어 처리**: 뜻이 다르면 원형 뒤에 (의미)를 붙여 구분 (예: 배(과일), 배(선박)).
    6. **인명/지명 구분**: 사람 이름이나 지역 이름은 품사를 '고유명사'로 표기하거나 원형 뒤에 (인명)/(지명)을 붙여 구분.
    7. **출력**: JSON 포맷 엄수.

    [JSON 예시]
    [{{"original_word": "배를", "root_word": "배(과일)", "origin": "고", "pos": "명사"}}, 
     {{"original_word": "서울에", "root_word": "서울", "origin": "한", "pos": "고유명사"}}]
    """
    
    if image_bytes:
        try: return json.loads(re.search(r'\[.*\]', api_call_direct(prompt + "\n(이미지 OCR 결과 참고)", image_bytes), re.DOTALL).group())
        except: return []
    else:
        chunks = split_text_smartly(text)
        res_list = []
        for chunk in chunks:
            r = api_call_direct(prompt + f"\n[분석할 텍스트]:\n{chunk}")
            if r: res_list.append(r)
        
        full_res = []
        for r in res_list:
            try: full_res.extend(json.loads(re.search(r'\[.*\]', r, re.DOTALL).group()))
            except: pass
        return full_res

# =========================================================
# [4] 파일 처리 & 노이즈 제거 (꼬리말 제거 + BytesIO)
# =========================================================
def clean_noise_text(text):
    """[기능] 꼬리말/메타데이터 제거"""
    if not text: return ""
    lines = text.split('\n')
    cleaned_lines = []
    patterns = [r'\.indd', r'\d{4}-\d{2}-\d{2}', r'오후\s*\d+:\d+', r'오전\s*\d+:\d+']
    for line in lines:
        is_noise = False
        for p in patterns:
            if re.search(p, line): is_noise = True; break
        if not is_noise: cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()

def extract_text_unified(file_bytes, file_type, page_idx):
    debug = st.session_state.get('debug_mode', False)
    if debug: log_debug(f"추출 시작: Page {page_idx}", "info")

    raw_text = ""
    
    if "image" in file_type:
        raw_text = api_call_vision_ocr(file_bytes)
    elif "pdf" in file_type:
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    if page_idx < len(pdf.pages):
                        page = pdf.pages[page_idx]
                        crop_box = (0, page.height * 0.05, page.width, page.height * 0.9)
                        try: raw_text = page.crop(crop_box).extract_text()
                        except: raw_text = page.extract_text()
            except: pass

        if (not raw_text or len(raw_text.strip()) < 5) and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc): raw_text = doc[page_idx].get_text()
            except: pass
        
        if (not raw_text or len(raw_text.strip()) < 30) and FITZ_AVAILABLE:
            try:
                if debug: log_debug("OCR 전환", "warn")
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc):
                    pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                    raw_text = api_call_vision_ocr(pix.tobytes("png"))
            except: pass
    
    return clean_noise_text(raw_text) if raw_text else ""

def get_page_image_bytes(file_bytes, file_type, page_idx):
    if "image" in file_type: return file_bytes
    elif "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            if 0 <= page_idx < len(doc):
                return doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0)).tobytes("png")
        except: pass
    return None

# =========================================================
# [5] 데이터 저장 & 병합 (덮어쓰기 방지 + 횟수 합산)
# =========================================================
def clean_val(v):
    if isinstance(v, str): return v.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('👤 ', '')
    return v

def calc_freq(row):
    """쪽수 컬럼 기반 빈도수 합산"""
    total = 0
    for c in row.index:
        if str(c).startswith('쪽수'):
            v = str(row[c])
            if '_' in v: # "139_2" 형태 처리
                try: total += int(v.split('_')[1])
                except: total += 1
            elif v not in ['nan', '', 'None']: total += 1
    return total

def merge_master_data(old_df, new_df):
    """[해결] 엑셀 이어하기 시 데이터 덮어쓰기 방지 및 횟수/쪽수 병합"""
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
            if pd.notna(val) and str(val) != 'nan': pages.append(str(val))
            
        for c in page_cols_new:
            val = row.get(f"{c}_new", row.get(c))
            if pd.notna(val) and str(val) != 'nan': pages.append(str(val))
            
        for i, p in enumerate(pages):
            new_row[f"쪽수{i+1}"] = p
            
        final_rows.append(new_row)
        
    result_df = pd.DataFrame(final_rows)
    result_df['출연횟수'] = result_df.apply(calc_freq, axis=1) # 횟수 재계산
    
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    result_df['sk'] = result_df['구분'].map(sort_map).fillna(5)
    result_df = result_df.sort_values(['sk', '자료']).drop('sk', axis=1)
    
    return result_df

def save_logic(edited_df, page_str, sheet_obj, context_text):
    if sheet_obj:
        logs = []
        for _, row in edited_df.iterrows():
            if not row['delete_check']:
                logs.append([datetime.now().isoformat(), row['original_word'], row['root_word'], clean_val(row['origin']), clean_val(row['pos']), 'modify', context_text[:50]])
        if logs: send_data_with_retry(sheet_obj, logs, True)

    valid = edited_df[edited_df['delete_check'] == False].copy()
    valid['n_cnt'] = valid['count'].apply(lambda x: int(re.sub(r'[^0-9]', '', str(x))) if re.search(r'\d', str(x)) else 1)
    
    # 동음이의어/인명지명 구분 위해 groupby 키에 pos 추가
    agg = valid.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({'n_cnt': 'sum'})
    
    temp_rows = []
    for _, item in agg.iterrows():
        root, origin, cnt = item['root_word'], clean_val(item['origin']), item['n_cnt']
        val = f"{page_str}_{cnt}" if cnt > 1 else page_str
        temp_rows.append({'구분': origin, '자료': root, '쪽수1': val})
    
    st.session_state.master_df = merge_master_data(st.session_state.master_df, pd.DataFrame(temp_rows))
    save_backup_to_cloud(st.session_state.last_mode, st.session_state.master_df) # 자동 백업
    return True

# =========================================================
# [6] 메인 UI
# =========================================================
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'file_hash' not in st.session_state: st.session_state.file_hash = None
if 'file_bytes_cache' not in st.session_state: st.session_state.file_bytes_cache = None
if 'start_page_offset' not in st.session_state: st.session_state.start_page_offset = 1
if 'main_editor_area' not in st.session_state: st.session_state.main_editor_area = ""

st.title("📝 국어활동 AI 분석기")

with st.sidebar:
    st.header("⚙️ 설정")
    mode = st.radio("언어 모드", ["🇰🇷 표준어", "🇰🇵 문화어"])
    MODE_KEY = "SOUTH" if "표준어" in mode else "NORTH"
    
    st.markdown("---")
    st.session_state.debug_mode = st.checkbox("🛠️ 디버깅 모드")
    
    if 'last_mode' not in st.session_state: st.session_state.last_mode = MODE_KEY
    if st.session_state.last_mode != MODE_KEY:
        if st.session_state.master_df is not None: save_backup_to_cloud(st.session_state.last_mode, st.session_state.master_df)
        st.session_state.master_df = None
        st.session_state.last_mode = MODE_KEY
        st.rerun()
        
    sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)
    if sheet: st.caption(f"✅ 학습 데이터: {len(sheet_data)}건")
    
    st.markdown("---")
    up_excel = st.file_uploader("📂 엑셀 이어하기", type=['xlsx'])
    if up_excel:
        if st.button("병합하기"):
            try:
                loaded = pd.read_excel(up_excel)
                if st.session_state.master_df is not None:
                    st.session_state.master_df = merge_master_data(st.session_state.master_df, loaded)
                else:
                    st.session_state.master_df = loaded
                st.success("안전하게 병합되었습니다."); time.sleep(1); st.rerun()
            except Exception as e: st.error(f"오류: {e}")

    if st.button("🔄 클라우드 복구"):
        r = load_backup_from_cloud(MODE_KEY)
        if r is not None: st.session_state.master_df = r; st.rerun()

    with st.expander("➕ 수동 추가"):
        with st.form("manual"):
            o = st.text_input("원본"); r = st.text_input("원형")
            org = st.selectbox("분류", ["고","한","외","혼"]); p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사","고유명사"]) # 고유명사 추가
            if st.form_submit_button("추가"): 
                new_entry = {
                    "delete_check": False, "count": "1회", "original_word": o,
                    "root_word": r, "origin": f"🔵 {org}" if org=='고' else org, 
                    "pos": f"📦 {p}" if p=='명사' else p
                }
                st.session_state.analysis_result.append(new_entry)
                send_data_with_retry(sheet, [datetime.now().isoformat(), o, r, org, p, 'add', '수동'])
                st.toast("추가되었습니다."); st.rerun()

st.subheader("1. 분석 자료 입력")
main_file = st.file_uploader("PDF/이미지 파일", type=['pdf', 'png', 'jpg'])

if main_file:
    current_bytes = main_file.getvalue()
    file_hash = hashlib.md5(current_bytes).hexdigest()
    
    if st.session_state.file_hash != file_hash:
        st.session_state.file_hash = file_hash
        st.session_state.file_bytes_cache = current_bytes
        st.session_state.page_idx = 0
        st.session_state.analysis_result = []
        st.session_state.debug_logs = []
        
        extracted = extract_text_unified(current_bytes, main_file.type, 0)
        st.session_state.main_editor_area = extracted
        log_debug(f"파일 로드 완료", "success")
        st.rerun()
    
    file_bytes = st.session_state.file_bytes_cache
    file_type = main_file.type
else:
    file_bytes = None

if st.session_state.debug_mode and st.session_state.debug_logs:
    with st.expander("🔍 상세 처리 로그", expanded=True):
        for log in st.session_state.debug_logs: st.markdown(log, unsafe_allow_html=True)

total_pages = 1
if file_bytes and "pdf" in main_file.type and FITZ_AVAILABLE:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    total_pages = len(doc)

col_v, col_i = st.columns([1, 1])

with col_v:
    if file_bytes:
        st.info("📷 미리보기")
        img = get_page_image_bytes(file_bytes, file_type, st.session_state.page_idx)
        if img: st.image(img, use_container_width=True)
        
        if "pdf" in file_type:
            c1, c2, c3 = st.columns([1, 2, 1])
            with c1:
                if st.button("◀"):
                    st.session_state.page_idx = max(0, st.session_state.page_idx - 1)
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, main_file.type, st.session_state.page_idx)
                    st.rerun()
            with c3:
                if st.button("▶"):
                    st.session_state.page_idx = min(total_pages - 1, st.session_state.page_idx + 1)
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, main_file.type, st.session_state.page_idx)
                    st.rerun()
            with c2:
                target = st.number_input("이동", 1, total_pages, st.session_state.page_idx+1)
                if target-1 != st.session_state.page_idx:
                    st.session_state.page_idx = target-1
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, main_file.type, st.session_state.page_idx)
                    st.rerun()
            
            st.session_state.start_page_offset = st.number_input("시작 쪽수(오프셋)", value=st.session_state.start_page_offset)
            page_str = str(st.session_state.page_idx + st.session_state.start_page_offset)
            st.caption(f"(현재 {st.session_state.page_idx+1}쪽 / 총 {total_pages}쪽) ➡️ 저장: {page_str}쪽")
        else:
            page_str = st.text_input("쪽수", value="1")
    else:
        st.info("파일 없음")
        page_str = st.text_input("쪽수", value="1")

with col_i:
    st.info("📝 분석 내용 (수정 가능)")
    txt_val = st.text_area("텍스트", height=500, key="main_editor_area")

    if st.button("🚀 분석 실행", type="primary", use_container_width=True):
        if not txt_val.strip(): st.warning("내용 없음")
        else:
            with st.spinner("분석 중..."):
                s_img = img if (file_bytes and len(txt_val)<30) else None
                res = get_analysis_hybrid(txt_val, s_img, sheet_data, MODE_KEY)
                
                proc = []
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                
                cnts = Counter([(r.get('root_word', ''), r.get('origin', '혼'), r.get('pos', '명사')) for r in res])
                seen = set()
                
                for r in res:
                    root = r.get('root_word', '')
                    ro = r.get('origin', '혼')
                    rp = r.get('pos', '명사')
                    row = r.get('original_word', '미상')
                    
                    key = (root, ro, rp)
                    if key not in seen:
                        proc.append({
                            "delete_check": False,
                            "count": f"{cnts[key]}회", 
                            "original_word": row,
                            "root_word": root,
                            "origin": om.get(ro, ro),
                            "pos": pm.get(rp, rp)
                        })
                        seen.add(key)
                st.session_state.analysis_result = proc

if st.session_state.analysis_result:
    st.divider()
    st.subheader("2. 결과 확인")
    edited = st.data_editor(
        pd.DataFrame(st.session_state.analysis_result),
        column_config={
            "delete_check": st.column_config.CheckboxColumn("삭제"),
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사", "고유명사"]) # 고유명사 옵션 추가
        },
        num_rows="dynamic",
        use_container_width=True,
        key="editor_key"
    )
    
    if not edited.equals(pd.DataFrame(st.session_state.analysis_result)):
        st.session_state.analysis_result = edited.to_dict('records')
    
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("⛔ 삭제"):
            dels = edited[edited['delete_check']==True]
            if not dels.empty and get_google_sheet_client():
                l = [[datetime.now().isoformat(), r['original_word'], r['root_word'], "", "", 'delete', 'User'] for _, r in dels.iterrows()]
                send_data_with_retry(get_google_sheet_client().open(SHEET_NAME).worksheet("South_Korea" if MODE_KEY=="SOUTH" else "North_Korea"), l, True)
            
            st.session_state.analysis_result = edited[edited['delete_check']==False].to_dict('records')
            st.rerun()
    with c2:
        if st.button("💾 저장+이동 (▶)"):
            if save_logic(edited, page_str, get_google_sheet_client().open(SHEET_NAME).worksheet("South_Korea" if MODE_KEY=="SOUTH" else "North_Korea"), txt_val):
                st.toast("저장 및 백업 완료")
                if file_bytes and "pdf" in main_file.type and st.session_state.page_idx < total_pages-1:
                    st.session_state.page_idx += 1
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, main_file.type, st.session_state.page_idx)
                    st.session_state.analysis_result = []
                    time.sleep(0.5); st.rerun()
    with c3:
        if st.button("💾 저장만"):
            if save_logic(edited, page_str, get_google_sheet_client().open(SHEET_NAME).worksheet("South_Korea" if MODE_KEY=="SOUTH" else "North_Korea"), txt_val): st.success("저장 완료")

if st.session_state.master_df is not None:
    st.markdown("---")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w: st.session_state.master_df.to_excel(w, index=False)
    st.download_button("📥 전체 엑셀 다운로드", buf.getvalue(), "final.xlsx")