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
    page_title="국어활동 AI 분석기 (Complete)", 
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
TRUST_THRESHOLD = 3 

# [CSS] 다크 모드 스타일
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

# [디버깅] 로그 함수
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
# [3] AI 엔진
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    for row in sheet_data[-50:]:
        if row.get('action') == 'delete': rules.append(f"- [삭제]: '{row.get('original_word')}' 제외")
        elif row.get('action') in ['add', 'modify']: rules.append(f"- [고정]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}'")
    return "\n[사용자 규칙]:\n" + "\n".join(rules) + "\n" if rules else ""

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
        res = requests.post(url, headers=headers, json={"contents": [{"parts": parts}]}, timeout=60)
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

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    prompt = f"""
    당신은 '{"대한민국 표준어" if mode_key=="SOUTH" else "북한 문화어"}' 형태소 분석 전문가입니다.
    {generate_prompt_from_sheet(sheet_data)}
    [Chain of Thought]
    1. 문맥 파악. 2. 조사/어미 제거. 3. '하다' 용언 처리. 4. 품사 필터링.
    5. 출력: JSON 포맷 엄수.
    [JSON 예시]
    [{{"original_word": "배를", "root_word": "배", "origin": "고", "pos": "명사"}}]
    """
    
    if image_bytes:
        try: return json.loads(re.search(r'\[.*\]', api_call_direct(prompt, image_bytes), re.DOTALL).group())
        except: return []
    else:
        chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
        res_list = []
        for chunk in chunks:
            r = api_call_direct(prompt + f"\n[텍스트]:\n{chunk}")
            if r: res_list.append(r)
        
        full_res = []
        for r in res_list:
            try: full_res.extend(json.loads(re.search(r'\[.*\]', r, re.DOTALL).group()))
            except: pass
        return full_res

# =========================================================
# [4] 파일 처리 및 노이즈 제거 (기능 추가됨)
# =========================================================
def clean_noise_text(text):
    """
    [신규 기능] 파일 정보, 날짜, 쪽수 등 불필요한 꼬리말 제거
    """
    if not text: return ""
    lines = text.split('\n')
    cleaned_lines = []
    
    # 제거할 패턴 정의 (indd 파일명, 날짜 형식)
    patterns = [
        r'\.indd',           # 인디자인 파일 확장자
        r'\d{4}-\d{2}-\d{2}', # 날짜 (2024-06-26)
        r'오후\s*\d+:\d+',    # 시간 (오후 3:07)
        r'오전\s*\d+:\d+'     # 시간
    ]
    
    for line in lines:
        is_noise = False
        # 패턴 매칭 확인
        for p in patterns:
            if re.search(p, line):
                is_noise = True
                break
        
        # 꼬리말(단독 숫자) 제거 - 필요시 해제 가능
        # if line.strip().isdigit() and int(line.strip()) > 0: is_noise = True 
        
        if not is_noise:
            cleaned_lines.append(line)
            
    return "\n".join(cleaned_lines).strip()

def extract_text_unified(file_bytes, file_type, page_idx):
    debug = st.session_state.get('debug_mode', False)
    if debug: log_debug(f"추출 시작: Page {page_idx}", "info")

    raw_text = ""
    
    if "image" in file_type:
        raw_text = api_call_vision_ocr(file_bytes)
        
    elif "pdf" in file_type:
        # (1) PDFPlumber
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    if page_idx < len(pdf.pages):
                        raw_text = pdf.pages[page_idx].extract_text()
                        if raw_text and len(raw_text.strip()) > 30:
                            if debug: log_debug(f"Plumber 성공 ({len(raw_text)}자)", "success")
            except: pass

        # (2) Fitz
        if (not raw_text or len(raw_text.strip()) < 5) and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc):
                    raw_text = doc[page_idx].get_text()
                    if raw_text and len(raw_text.strip()) > 30:
                        if debug: log_debug(f"Fitz 성공 ({len(raw_text)}자)", "success")
            except: pass
        
        # (3) Vision OCR
        if (not raw_text or len(raw_text.strip()) < 30) and FITZ_AVAILABLE:
            try:
                if debug: log_debug("이미지 변환 후 OCR 시도", "warn")
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc):
                    pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                    raw_text = api_call_vision_ocr(pix.tobytes("png"))
            except Exception as e:
                if debug: log_debug(f"변환 오류: {e}", "err")
    
    # [최종 단계] 노이즈 제거 후 반환
    final_text = clean_noise_text(raw_text) if raw_text else ""
    return final_text

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
# [5] 데이터 저장
# =========================================================
def clean_val(v):
    if isinstance(v, str): return v.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('👤 ', '')
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
                logs.append([datetime.now().isoformat(), row['original_word'], row['root_word'], clean_val(row['origin']), clean_val(row['pos']), 'modify', context_text[:50]])
        if logs: send_data_with_retry(sheet_obj, logs, True)

    valid = edited_df[edited_df['delete_check'] == False].copy()
    valid['n_cnt'] = valid['count'].apply(lambda x: int(str(x).replace('회','').strip()) if '회' in str(x) else 1)
    
    agg = valid.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({'n_cnt': 'sum', 'original_word': lambda x: ', '.join(x.unique())})
    
    if st.session_state.master_df is None:
        st.session_state.master_df = pd.DataFrame(columns=['구분', '자료', '출연횟수', '쪽수1'])
    master = st.session_state.master_df
    
    new_rows = []
    for _, item in agg.iterrows():
        root, origin, cnt = item['root_word'], clean_val(item['origin']), item['n_cnt']
        val = f"{page_str}_{cnt}" if cnt > 1 else page_str
        mask = (master['자료'] == root) & (master['구분'] == origin)
        
        if mask.any():
            idx = master[mask].index[0]
            filled = [c for c in master.columns if '쪽수' in c and pd.notna(master.at[idx, c])]
            next_c = f"쪽수{len(filled)+1}"
            if next_c not in master.columns: master[next_c] = None
            master.at[idx, next_c] = val
        else:
            new_rows.append({'구분': origin, '자료': root, '출연횟수': 0, '쪽수1': val})
            
    if new_rows: master = pd.concat([master, pd.DataFrame(new_rows)], ignore_index=True)
    master['출연횟수'] = master.apply(calc_freq, axis=1)
    
    sm = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    master['sk'] = master['구분'].map(sm).fillna(5)
    master = master.sort_values(['sk', '자료']).drop('sk', axis=1)
    
    st.session_state.master_df = master
    save_backup_to_cloud(st.session_state.last_mode, master)
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
# [핵심] 텍스트 에디터 키 (동적 관리)
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
                    st.session_state.master_df = pd.concat([st.session_state.master_df, loaded]).drop_duplicates(subset=['자료', '구분'], keep='first')
                else: st.session_state.master_df = loaded
                st.success("완료"); time.sleep(1); st.rerun()
            except: st.error("오류")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("☁️ 백업"): save_backup_to_cloud(MODE_KEY, st.session_state.master_df)
    with c2:
        if st.button("🔄 복구"):
            r = load_backup_from_cloud(MODE_KEY)
            if r is not None: st.session_state.master_df = r; st.rerun()

    with st.expander("➕ 수동 추가"):
        with st.form("manual"):
            o = st.text_input("원본"); r = st.text_input("원형")
            org = st.selectbox("분류", ["고","한","외","혼"]); p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사"])
            if st.form_submit_button("추가"): send_data_with_retry(sheet, [datetime.now().isoformat(), o, r, org, p, 'add', '수동'])

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
        
        # [데이터 주입] 추출된 텍스트를 세션 키에 할당
        extracted = extract_text_unified(current_bytes, main_file.type, 0)
        st.session_state.main_editor_area = extracted
        log_debug(f"파일 로드 및 텍스트 추출 완료 ({len(extracted)}자)", "success")
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
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, file_type, st.session_state.page_idx)
                    st.rerun()
            with c3:
                if st.button("▶"):
                    st.session_state.page_idx = min(total_pages - 1, st.session_state.page_idx + 1)
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, file_type, st.session_state.page_idx)
                    st.rerun()
            with c2:
                target = st.number_input("이동", 1, total_pages, st.session_state.page_idx+1)
                if target-1 != st.session_state.page_idx:
                    st.session_state.page_idx = target-1
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, file_type, st.session_state.page_idx)
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
    # [핵심] value 파라미터 대신 key로 상태 동기화
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
                if file_bytes and "pdf" in main_file.type and st.session_state.page_idx < total_pages-1:
                    st.session_state.page_idx += 1
                    # 이동 시에도 키 직접 할당
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, main_file.type, st.session_state.page_idx)
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