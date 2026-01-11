import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import os
import time
import base64
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from collections import Counter

# =========================================================
# [0] 라이브러리 임포트 및 상태 체크 (GP9 원본 유지)
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

# =========================================================
# ⚙️ 설정 (GP9 원본 유지)
# =========================================================
st.set_page_config(
    page_title="국어활동 AI 분석기 (Ultimate Fixed)", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="expanded"
)

try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = ""
except:
    API_KEY = ""

MODEL_NAME = "gemini-2.0-flash-exp"
SHEET_NAME = "Korean_DB"

# [CSS] 다크 모드 및 스타일 (사용자 편의성 유지)
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
    </style>
""", unsafe_allow_html=True)

# =========================================================
# 🔐 구글 시트 연결 (GP9 원본 유지)
# =========================================================
@st.cache_resource
def get_google_sheet_client():
    try:
        if "gcp_service_account" not in st.secrets:
            return None
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
    except Exception as e:
        return None, []

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
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return False
    return False

# [백업 기능] 클라우드 백업 (자동화)
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
        
        # DataFrame을 문자열로 변환하여 업로드 (오류 방지)
        df_str = df.fillna("").astype(str)
        data_to_upload = [df_str.columns.values.tolist()] + df_str.values.tolist()
        worksheet.update(data_to_upload)
        return True
    except Exception as e:
        return False

# [백업 기능] 클라우드 복구
def load_backup_from_cloud(mode_key):
    client = get_google_sheet_client()
    if not client: return None
    
    backup_sheet_name = f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}"
    try:
        spreadsheet = client.open(SHEET_NAME)
        worksheet = spreadsheet.worksheet(backup_sheet_name)
        data = worksheet.get_all_records()
        if not data: return None
        return pd.DataFrame(data)
    except: return None

# =========================================================
# 🧠 AI 및 전처리 로직 (7단계 반영 + 동음이의어/인명지명 추가)
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    # [7단계] 최근 학습된 내용(뒤에서 50개)을 가장 우선순위로 반영
    for row in sheet_data[-50:]:
        if row.get('action') == 'delete':
            rules.append(f"- [삭제 규칙]: '{row.get('original_word')}'는 분석 결과에서 제외하세요.")
        elif row.get('action') in ['add', 'modify']:
            rules.append(f"- [고정 규칙]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}', 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    
    if rules:
         return "\n[🚨 최우선 사용자 학습 규칙 (이것이 법이다)]:\n" + "\n".join(rules) + "\n"
    return ""

def api_call_direct(prompt, image_bytes=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    parts = [{"text": prompt}]
    
    # 이미지 처리 추가
    if image_bytes:
        b64_image = base64.b64encode(image_bytes).decode('utf-8')
        parts.append({"inline_data": {"mime_type": "image/png", "data": b64_image}})

    data = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0.1}}
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code != 200:
            return None
        result_json = response.json()
        if 'candidates' in result_json:
            text_res = result_json['candidates'][0]['content']['parts'][0]['text']
            return text_res
        return None
    except Exception as e:
        return None

def api_call_vision_ocr(image_bytes):
    # GP9의 OCR 프롬프트 로직 복원
    prompt_text = """
    이 이미지에 있는 텍스트를 보이는 그대로 추출해주세요.
    말풍선, 단락은 줄바꿈으로 구분하세요.
    쪽수, 머리말 같은 노이즈는 제외하세요.
    """
    res = api_call_direct(prompt_text, image_bytes)
    return res if res else ""

def split_text_smartly(text, chunk_size=1000):
    # [복구] 긴 텍스트를 문장 부호 기준으로 안전하게 자르는 기능
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
    
    # [8단계] 정확도 향상을 위한 상세 프롬프트 (동음이의어, 인명/지명 추가)
    prompt = f"""
    당신은 '{mode_desc}' 국어학 및 어원 분석 전문가입니다.
    {learning_prompt}
    
    [분석 단계 (Chain of Thought)]
    1. **문맥 파악**: '{mode_desc}' 규칙을 적용하여 문맥을 파악하세요.
    2. **형태소 분리**: 조사(은/는/이/가 등)와 어미를 제거하고 실질 형태소만 남기세요.
    3. **'하다' 용언 처리**: '공부하다' -> '공부'(명사)로 처리. 동사/형용사가 아니라 명사로 분류하세요.
    4. **품사 필터링**: 명사, 동사, 형용사, 부사, 관형사, 대명사만 남기세요. (조사, 의존명사 제외)
    5. **동음이의어 처리**: 뜻이 다르면 원형 뒤에 (의미)를 붙여 구분하세요. (예: 배(과일), 배(선박))
    6. **인명/지명 처리**: 사람 이름이나 지역 이름은 품사를 '고유명사'로 표기하세요.
    7. **출력**: JSON 포맷 엄수.

    [JSON 예시]
    [
        {{"original_word": "배를", "root_word": "배(과일)", "origin": "고", "pos": "명사"}},
        {{"original_word": "서울에", "root_word": "서울", "origin": "한", "pos": "고유명사"}}
    ]
    """
    
    if image_bytes:
        full_res = api_call_direct(prompt + "\n(이미지 OCR 결과 참고)", image_bytes)
        try:
            match = re.search(r'\[.*\]', full_res, re.DOTALL)
            return json.loads(match.group()) if match else []
        except: return []
    else:
        chunks = split_text_smartly(text)
        all_results = []
        for chunk in chunks:
            if not chunk.strip(): continue
            chunk_res = api_call_direct(prompt + f"\n[분석할 텍스트]:\n{chunk}")
            if chunk_res:
                try:
                    match = re.search(r'\[.*\]', chunk_res, re.DOTALL)
                    if match: all_results.extend(json.loads(match.group()))
                except: pass
            time.sleep(0.1)
        return all_results

# =========================================================
# [4] 파일 처리 & 노이즈 제거 (GP9 + 안정성)
# =========================================================
def clean_noise_text(text):
    """[복구] 파일 정보, 날짜, 시간 등 꼬리말 제거"""
    if not text: return ""
    lines = text.split('\n')
    cleaned_lines = []
    # 제거할 패턴 (indd 파일명, 날짜 2024-xx-xx, 시간 등)
    patterns = [r'\.indd', r'\d{4}-\d{2}-\d{2}', r'오후\s*\d+:\d+', r'오전\s*\d+:\d+']
    for line in lines:
        is_noise = False
        for p in patterns:
            if re.search(p, line): is_noise = True; break
        if not is_noise: cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()

def extract_text_unified(file_bytes, file_type, page_idx):
    """BytesIO 복제 방식 (파일 닫힘 오류 해결)"""
    raw_text = ""
    if "image" in file_type:
        raw_text = api_call_vision_ocr(file_bytes)
    elif "pdf" in file_type:
        # 1. PDFPlumber (영역 크롭)
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    if page_idx < len(pdf.pages):
                        page = pdf.pages[page_idx]
                        crop_box = (0, page.height * 0.05, page.width, page.height * 0.9)
                        try: raw_text = page.crop(crop_box).extract_text()
                        except: raw_text = page.extract_text()
            except: pass
        
        # 2. Fitz (텍스트 레이어 백업)
        if (not raw_text or len(raw_text.strip()) < 5) and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc): raw_text = doc[page_idx].get_text()
            except: pass
        
        # 3. Vision OCR (이미지형 PDF 백업)
        if (not raw_text or len(raw_text.strip()) < 30) and FITZ_AVAILABLE:
            try:
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
# [5] 데이터 저장 & 병합 (1번 문제 해결: 스마트 병합)
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
    """
    [핵심 해결] 엑셀 이어하기 시 데이터 덮어쓰기 방지 및 횟수/쪽수 병합
    - 기존 데이터와 새 데이터를 '자료', '구분' 기준으로 합침
    - 쪽수 컬럼을 모두 모아서 중복을 제거하고 다시 나열함
    """
    if old_df is None or old_df.empty: return new_df
    
    key_cols = ['자료', '구분']
    # Outer join으로 합침
    merged = pd.merge(old_df, new_df, on=key_cols, how='outer', suffixes=('_old', '_new'))
    
    page_cols_old = [c for c in old_df.columns if c.startswith('쪽수')]
    page_cols_new = [c for c in new_df.columns if c.startswith('쪽수')]
    
    final_rows = []
    for _, row in merged.iterrows():
        new_row = {k: row[k] for k in key_cols}
        
        # 쪽수 데이터 수집 (리스트로 모음)
        pages = []
        
        for c in page_cols_old:
            val = row.get(f"{c}_old", row.get(c))
            if pd.notna(val) and str(val) != 'nan' and str(val) != '': pages.append(str(val))
            
        for c in page_cols_new:
            val = row.get(f"{c}_new", row.get(c))
            if pd.notna(val) and str(val) != 'nan' and str(val) != '': pages.append(str(val))
            
        # [중복 제거] 쪽수 중복 방지 (set 사용)
        unique_pages = sorted(list(set(pages)))
        
        # 쪽수 컬럼 재할당
        for i, p in enumerate(unique_pages):
            new_row[f"쪽수{i+1}"] = p
            
        final_rows.append(new_row)
        
    result_df = pd.DataFrame(final_rows)
    result_df['출연횟수'] = result_df.apply(calc_freq, axis=1) # 횟수 재계산
    
    # 정렬
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
    
    # [동음이의어/고유명사 구분] groupby 키에 pos 추가
    agg = valid.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({'n_cnt': 'sum'})
    
    temp_rows = []
    for _, item in agg.iterrows():
        root, origin, cnt = item['root_word'], clean_val(item['origin']), clean_val(item['pos']), item['n_cnt']
        val = f"{page_str}_{cnt}" if cnt > 1 else page_str
        temp_rows.append({'구분': origin, '자료': root, '쪽수1': val}) # pos는 키로만 쓰고 엑셀엔 미포함? (GP9 확인 필요 -> 일단 엑셀엔 구분/자료/쪽수만 들어가는게 기본 구조)
    
    # 스마트 병합 실행
    st.session_state.master_df = merge_master_data(st.session_state.master_df, pd.DataFrame(temp_rows))
    
    # [자동 백업]
    save_backup_to_cloud(st.session_state.last_mode, st.session_state.master_df)
    return True

# =========================================================
# [6] 메인 UI 구성
# =========================================================
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'file_hash' not in st.session_state: st.session_state.file_hash = None
if 'file_bytes_cache' not in st.session_state: st.session_state.file_bytes_cache = None
if 'start_page_offset' not in st.session_state: st.session_state.start_page_offset = 1
# 텍스트 에디터 동기화용 키
if 'main_editor_area' not in st.session_state: st.session_state.main_editor_area = ""

st.title("📝 국어활동 AI 분석기")

with st.sidebar:
    st.header("⚙️ 설정")
    mode = st.radio("언어 모드", ["🇰🇷 표준어", "🇰🇵 문화어"])
    MODE_KEY = "SOUTH" if "표준어" in mode else "NORTH"
    
    if 'last_mode' not in st.session_state: st.session_state.last_mode = MODE_KEY
    if st.session_state.last_mode != MODE_KEY:
        if st.session_state.master_df is not None: save_backup_to_cloud(st.session_state.last_mode, st.session_state.master_df)
        st.session_state.master_df = None
        st.session_state.last_mode = MODE_KEY
        st.rerun()
        
    sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)
    if sheet: st.caption(f"✅ 학습 데이터: {len(sheet_data)}건")
    
    st.markdown("---")
    
    # [1번 해결] 엑셀 이어하기 (자동 병합)
    up_excel = st.file_uploader("📂 엑셀 이어하기 (자동 병합)", type=['xlsx'])
    if up_excel and up_excel.name != st.session_state.get('last_excel_name'):
        try:
            loaded = pd.read_excel(up_excel)
            if st.session_state.master_df is not None:
                st.session_state.master_df = merge_master_data(st.session_state.master_df, loaded)
            else:
                st.session_state.master_df = loaded
            st.session_state.last_excel_name = up_excel.name
            st.success("데이터가 안전하게 병합되었습니다."); time.sleep(1); st.rerun()
        except Exception as e: st.error(f"오류: {e}")

    # [요청 반영] 복구 버튼만 남김
    if st.button("🔄 클라우드 복구"):
        r = load_backup_from_cloud(MODE_KEY)
        if r is not None: st.session_state.master_df = r; st.rerun()

    with st.expander("➕ 수동 추가"):
        with st.form("manual"):
            o = st.text_input("원본"); r = st.text_input("원형")
            org = st.selectbox("분류", ["고","한","외","혼"]); p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사","고유명사"])
            if st.form_submit_button("추가"): 
                # [2번 해결] 수동 추가 시 화면 테이블에 즉시 반영
                new_entry = {
                    "delete_check": False, "count": "1회", "original_word": o,
                    "root_word": r, "origin": f"🔵 {org}" if org=='고' else org, 
                    "pos": f"📦 {p}" if p=='명사' else p
                }
                st.session_state.analysis_result.append(new_entry)
                send_data_with_retry(sheet, [datetime.now().isoformat(), o, r, org, p, 'add', '수동'])
                st.toast("추가되었습니다."); st.rerun()

    # [9단계 해결] 이력 검색 등 불필요 UI 삭제됨

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
        
        extracted = extract_text_unified(current_bytes, main_file.type, 0)
        st.session_state.main_editor_area = extracted
        st.rerun()
    
    file_bytes = st.session_state.file_bytes_cache
    file_type = main_file.type
else:
    file_bytes = None

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
            
            # [복구] 쪽수 계산 및 표시
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
                # [복구] 스마트 분할 + 동음이의어/인명 구분 프롬프트 적용
                res = get_analysis_hybrid(txt_val, s_img, sheet_data, MODE_KEY)
                
                proc = []
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                
                # [해결] 키를 (원형, 분류, 품사) 튜플로 사용하여 동음이의어/고유명사 구분
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
    
    # [해결] 수정 내용 상태 유지 (Key 바인딩)
    # [해결] 고유명사 선택 가능하도록 옵션 추가
    edited = st.data_editor(
        pd.DataFrame(st.session_state.analysis_result),
        column_config={
            "delete_check": st.column_config.CheckboxColumn("삭제"),
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사", "고유명사"]) 
        },
        num_rows="dynamic",
        use_container_width=True,
        key="editor_key"
    )
    
    # 데이터 에디터 변경 감지 시 세션 업데이트
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
                st.toast("저장 및 자동 백업 완료")
                if file_bytes and "pdf" in main_file.type and st.session_state.page_idx < total_pages-1:
                    st.session_state.page_idx += 1
                    st.session_state.main_editor_area = extract_text_unified(file_bytes, main_file.type, st.session_state.page_idx)
                    st.session_state.analysis_result = []
                    time.sleep(0.5); st.rerun()
    with c3:
        if st.button("💾 저장만"):
            if save_logic(edited, page_str, get_google_sheet_client().open(SHEET_NAME).worksheet("South_Korea" if MODE_KEY=="SOUTH" else "North_Korea"), txt_val): st.success("저장 완료")

# [9단계] 불필요한 하단 UI 삭제 (데이터 모아보기 X)
if st.session_state.master_df is not None:
    st.markdown("---")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w: st.session_state.master_df.to_excel(w, index=False)
    st.download_button("📥 전체 엑셀 다운로드", buf.getvalue(), "final.xlsx")