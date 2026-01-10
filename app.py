import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import os
import gspread
import base64
from oauth2client.service_account import ServiceAccountCredentials
from collections import Counter
from datetime import datetime
import time

# [라이브러리 상태 체크]
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
# ⚙️ 설정 & 세션 초기화 (최상단 배치 - 에러 방지)
# =========================================================
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk" 
except:
    API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk"

MODEL_NAME = "gemini-2.0-flash-exp"
SHEET_NAME = "Korean_DB" 
TRUST_THRESHOLD = 3 

st.set_page_config(page_title="국어활동 AI 분석기", page_icon="📝", layout="wide")

# [변수 초기화]
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = None
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'last_uploaded_file_name' not in st.session_state: st.session_state.last_uploaded_file_name = None
if 'analysis_source' not in st.session_state: st.session_state.analysis_source = None 
if 'uploaded_file' not in st.session_state: st.session_state.uploaded_file = None
if 'total_pages' not in st.session_state: st.session_state.total_pages = 0
if 'current_page_idx' not in st.session_state: st.session_state.current_page_idx = 0
if 'start_page_offset' not in st.session_state: st.session_state.start_page_offset = 1 
if 'manual_page_input' not in st.session_state: st.session_state.manual_page_input = "1"
if 'last_mode' not in st.session_state: st.session_state.last_mode = "SOUTH"
if 'excel_buffer' not in st.session_state: st.session_state.excel_buffer = None 

# =========================================================
# 🔐 구글 시트 연결
# =========================================================
@st.cache_resource
def get_google_sheet_client():
    try:
        if "gcp_service_account" not in st.secrets: return None
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict: creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
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
        sheet = spreadsheet.worksheet(target_sheet_name)
        return sheet, sheet.get_all_records()
    except: return None, []

def send_data_with_retry(sheet_obj, data, is_multiple=False):
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
            else: return False
    return False

# [백업 기능]
def save_backup_to_cloud(mode_key, df):
    client = get_google_sheet_client()
    if not client or df is None or df.empty: return False
    backup_sheet_name = f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}"
    try:
        spreadsheet = client.open(SHEET_NAME)
        try: worksheet = spreadsheet.worksheet(backup_sheet_name); worksheet.clear() 
        except: worksheet = spreadsheet.add_worksheet(title=backup_sheet_name, rows=1000, cols=20)
        data_to_upload = [df.columns.values.tolist()] + df.astype(str).values.tolist()
        worksheet.update(data_to_upload)
        return True
    except: return False

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
# 🧠 AI 및 전처리 로직
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    df = pd.DataFrame(sheet_data)
    if df.empty: return ""
    prompt_lines = []
    
    if 'action' in df.columns:
        deleted = df[df['action'] == 'delete']
        for _, row in deleted.tail(10).iterrows(): 
            prompt_lines.append(f"- [학습된 예외]: '{row['original_word']}'는 절대 분석하지 마세요.")
        added = df[df['action'] == 'add']
        for _, row in added.tail(10).iterrows():
            prompt_lines.append(f"- [학습된 필수]: '{row['original_word']}'는 무조건 포함하세요.")
        modified = df[df['action'] == 'modify']
        for _, row in modified.tail(15).iterrows():
             prompt_lines.append(f"- [학습된 수정]: '{row['original_word']}'가 나오면 무조건 원형:'{row['root_word']}', 분류:'{row['origin']}'로 처리하세요.")
             
    if prompt_lines:
         return "\n[🚨 최우선 사용자 학습 규칙]:\n" + "\n".join(prompt_lines) + "\n"
    return ""

def api_call_direct(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    
    # [안전장치 해제]
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
    ]
    
    data = {
        "contents": [{"parts": [{"text": prompt}]}], 
        "safetySettings": safety_settings,
        "generationConfig": {"temperature": 0.1}
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code != 200: return None
        result_json = response.json()
        if 'candidates' in result_json:
            text_res = result_json['candidates'][0]['content']['parts'][0]['text']
            json_match = re.search(r'\[.*\]', text_res, re.DOTALL)
            if json_match: return json.loads(json_match.group())
        return None
    except: return None

def api_call_vision_ocr(image_bytes):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    prompt_text = "이미지의 텍스트를 추출하세요. 줄바꿈을 준수하고, 중복 단어도 그대로 적으세요."
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
    ]
    
    data = {"contents": [{"parts": [{"text": prompt_text}, {"inline_data": {"mime_type": "image/png", "data": base64_image}}]}], "safetySettings": safety_settings}

    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code != 200: return f"OCR 실패"
        result_json = response.json()
        if 'candidates' in result_json: return result_json['candidates'][0]['content']['parts'][0]['text']
        return "텍스트 없음"
    except: return f"통신 오류"

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

def extract_text_unified(file_obj, page_index):
    file_type = file_obj.type
    if "image" in file_type:
        try: return api_call_vision_ocr(file_obj.getvalue())
        except: return "이미지 오류"
    elif "pdf" in file_type:
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(file_obj) as pdf:
                    if page_index < 0 or page_index >= len(pdf.pages): return ""
                    page = pdf.pages[page_index]
                    width, height = page.width, page.height
                    crop_box = (0, 0, width, height * 0.9)
                    try: text = page.crop(crop_box).extract_text()
                    except: text = page.extract_text()
                    if text and len(text.strip()) > 30: return text
            except: pass
        if FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                page = doc[page_index]
                rect = page.rect
                clip_rect = fitz.Rect(0, 0, rect.width, rect.height * 0.9)
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip_rect)
                return api_call_vision_ocr(pix.tobytes("png"))
            except: return "PDF 변환 오류"
    return "파일 읽기 실패"

def get_page_image_bytes(file_obj, page_index):
    file_type = file_obj.type
    if "image" in file_type: return file_obj.getvalue() 
    elif "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
            page = doc[page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)) 
            return pix.tobytes("png") 
        except: return None
    return None

def get_analysis_hybrid(text, sheet_data, mode_key):
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    role = "당신은 국어학 전문가입니다. 실질 형태소(알맹이 단어)를 분석하세요."
    if mode_key == "NORTH": instr = "[북한 문화어] 두음법칙 미적용"
    else: instr = "[대한민국 표준어] 두음법칙 준수"

    base_instruction = f"""
    {role}\n{instr}
    [규칙]
    1. '명사+하다' -> 명사만 추출.
    2. 조사, 어미, 문장부호, 감탄사, 인사말 제외.
    3. JSON 리스트 포맷만 출력.
    형식: [{{"original_word": "...", "root_word": "...", "origin": "고", "pos": "명사"}}]
    """
    
    chunks = split_text_smartly(text)
    all_results = []
    
    for i, chunk in enumerate(chunks):
        if not chunk.strip(): continue
        prompt = f"""{learning_prompt}\n{base_instruction}\n\n분석할 문장:\n"{chunk}" """
        chunk_result = api_call_direct(prompt)
        if chunk_result: all_results.extend(chunk_result)
        time.sleep(0.1)
    return all_results

def calculate_total_appearances(row):
    total = 0
    for col in row.index:
        if str(col).startswith('쪽수'):
            val = str(row[col])
            if '_' in val:
                try: total += int(val.split('_')[1])
                except: total += 1
            elif val != 'nan' and val != '': total += 1
    return total

def add_emoji_to_origin(val):
    mapping = {'고': '🔵 고', '한': '🟢 한', '외': '🔴 외', '혼': '🟣 혼'}
    return mapping.get(val, val)

def clean_value_for_save(val):
    if isinstance(val, str):
        return val.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('❗ ', '').replace('✅ ', '').replace('📝 ', '').replace('👤 ', '')
    return val

def get_problematic_words(sheet_data):
    problem_roots = set()
    if not sheet_data: return problem_roots
    for row in sheet_data:
        if row.get('action') in ['modify', 'delete', 'add']:
            if row.get('root_word'): problem_roots.add(str(row.get('root_word')))
            if row.get('original_word'): problem_roots.add(str(row.get('original_word')))
    return problem_roots

def check_trust_level_strict(root_word, uploaded_df, problematic_words):
    if root_word in problematic_words: return False
    if uploaded_df is None: return False
    if '자료' not in uploaded_df.columns: return False
    match = uploaded_df[uploaded_df['자료'] == root_word]
    if match.empty: return False
    try:
        count_val = match.iloc[0]['출연횟수']
        return count_val >= TRUST_THRESHOLD
    except: return False

def get_blacklist_from_sheet(sheet_data):
    blacklist = set()
    if not sheet_data: return blacklist
    for row in sheet_data:
        if row.get('action') == 'delete':
            blacklist.add(row.get('original_word'))
            blacklist.add(row.get('root_word'))
    return blacklist

def load_excel_safely(file):
    try:
        df = pd.read_excel(file)
        if '자료' in df.columns and '출연횟수' in df.columns: return df
        return None
    except: return None

# [핵심] 에디터 상태 동기화 함수 (추가 버튼 누를 때 데이터 날아감 방지)
def apply_editor_changes_safe(source_key):
    editor_key = f"editor_{source_key}"
    if editor_key in st.session_state and st.session_state.analysis_result:
        changes = st.session_state[editor_key].get("edited_rows", {})
        for idx, changes_dict in changes.items():
            if idx < len(st.session_state.analysis_result):
                for col, val in changes_dict.items():
                    st.session_state.analysis_result[idx][col] = val

# =========================================================
# 🔄 결과 표시 UI 함수 (기능 통합 & 다운로드 복원)
# =========================================================
def render_analysis_ui(page_str, source_key):
    if st.session_state.analysis_result:
        st.markdown("### 📊 분석 결과")
        df_disp = pd.DataFrame(st.session_state.analysis_result)
        
        col_conf = {
            "delete_check": st.column_config.CheckboxColumn("삭제", width="small"),
            "count": st.column_config.TextColumn("빈도"),
            "original_word": st.column_config.TextColumn("원본"),
            "root_word": "원형",
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사"])
        }
        
        edited = st.data_editor(
            df_disp[["delete_check","count","original_word","root_word","origin","pos"]], 
            column_config=col_conf, 
            use_container_width=True, 
            key=f"editor_{source_key}", 
            num_rows="fixed"
        )
        
        b1, b2, b3 = st.columns([1, 2, 2])
        sheet, sheet_data = get_sheet_data_fresh(st.session_state.last_mode) 

        def save_data(df, pg):
            valid = df[df['delete_check'] == False].copy()
            logs = []
            for _, r in valid.iterrows():
                logs.append([datetime.now().isoformat(), r['original_word'], r['root_word'], clean_value_for_save(r['origin']), clean_value_for_save(r['pos']), 'modify', 'save'])
            if sheet and logs: send_data_with_retry(sheet, logs, True)
            
            agg = valid.groupby(['root_word','origin','pos'], as_index=False).agg(
                {'original_word': lambda x: ','.join(x.unique()), 'count': lambda x: sum([int(str(v).replace('회','')) for v in x])}
            )
            
            bdf = st.session_state.master_df
            if bdf is None: bdf = pd.DataFrame(columns=['구분','자료','출연횟수'])
            
            new_rows = []
            for _, r in agg.iterrows():
                root = r['root_word']
                org = clean_value_for_save(r['origin'])
                cnt = r['count']
                p_str = f"{pg}_{cnt}" if cnt > 1 else pg
                
                mask = (bdf['자료'] == root) & (bdf['구분'] == org)
                if mask.any():
                    idx = bdf[mask].index[0]
                    filled = bdf.loc[idx].filter(like='쪽수').notna().sum()
                    bdf.at[idx, f"쪽수{filled+1}"] = p_str
                else:
                    new_rows.append({'구분': org, '자료': root, '쪽수1': p_str})
            
            if new_rows: bdf = pd.concat([bdf, pd.DataFrame(new_rows)], ignore_index=True)
            bdf['출연횟수'] = bdf.apply(calculate_total_appearances, axis=1)
            bdf['sort'] = bdf['구분'].map({'고':1, '순':1, '한':2, '외':3, '혼':4}).fillna(5)
            bdf = bdf.sort_values(['sort', '자료']).drop('sort', axis=1)
            st.session_state.master_df = bdf
            
            out = io.BytesIO()
            with pd.ExcelWriter(out, engine='openpyxl') as writer: bdf.to_excel(writer, index=False)
            out.seek(0)
            st.session_state.excel_buffer = out
            
            save_backup_to_cloud(st.session_state.last_mode, bdf)
            return True

        with b1:
            if st.button("⛔ 체크 삭제", key=f"btn_del_{source_key}", type="secondary"):
                dels = edited[edited['delete_check']==True]
                if not dels.empty and sheet:
                    logs = [[datetime.now().isoformat(), r['original_word'].split('(')[0], r['root_word'], "", "", 'delete', 'check_del'] for _, r in dels.iterrows()]
                    send_data_with_retry(sheet, logs, True)
                st.session_state.analysis_result = edited[edited['delete_check']==False].to_dict('records')
                st.rerun()
                
        with b2:
            if st.button("💾 저장하고 다음 쪽(▶)", key=f"btn_next_{source_key}", type="primary", use_container_width=True):
                save_data(edited, page_str)
                is_pdf = st.session_state.uploaded_file and "pdf" in st.session_state.uploaded_file.type
                if source_key == 'FILE' and is_pdf and st.session_state.current_page_idx < st.session_state.total_pages - 1:
                    st.session_state.current_page_idx += 1
                    st.session_state.analysis_result = None
                    st.rerun()
                else: st.success("저장되었습니다.")

        with b3:
            if st.button("💾 저장만 하기 (종료)", key=f"btn_save_{source_key}", use_container_width=True):
                save_data(edited, page_str)
                st.success("✅ 저장되었습니다.")
        
        # [복원] 엑셀 다운로드 버튼
        if st.session_state.excel_buffer:
            st.download_button("📥 엑셀파일 다운로드", st.session_state.excel_buffer, "국어활동_분석결과_통합.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"dl_{source_key}", type="secondary", use_container_width=True)

# =========================================================
# 🖥️ 메인 화면
# =========================================================
st.title("📝 국어활동 AI 분석기")

with st.sidebar:
    st.header("🏳️ 분석 모드")
    mode_selection = st.radio("언어 환경", ("🇰🇷 대한민국 표준어", "🇰🇵 북한 문화어"))
    MODE_KEY = "SOUTH" if "대한민국" in mode_selection else "NORTH"
    
    if st.session_state.last_mode != MODE_KEY:
        st.session_state.analysis_result = None
        if st.session_state.master_df is not None:
            save_backup_to_cloud(st.session_state.last_mode, st.session_state.master_df)
        st.session_state.master_df = None 
        st.session_state.last_uploaded_file_name = None 
        st.session_state.last_mode = MODE_KEY
        st.rerun()

    sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)
    if sheet: st.success(f"✅ {len(sheet_data)}건의 학습 데이터 연동됨")
    else: st.error("❌ 서버 연결 실패")

    st.markdown("---")
    uploaded_excel = st.file_uploader("📂 작업 파일 불러오기 (Excel)", type=['xlsx'])
    
    # [1. 데이터 덮어쓰기 방지 해결]
    if uploaded_excel and uploaded_excel.name != st.session_state.last_uploaded_file_name:
        udf = load_excel_safely(uploaded_excel)
        if udf is not None:
             if st.session_state.master_df is not None:
                 # 기존 데이터가 있으면 합침
                 st.session_state.master_df = pd.concat([st.session_state.master_df, udf]).drop_duplicates(subset=['자료', '구분']).reset_index(drop=True)
             else: 
                 # 없으면 새로 할당
                 st.session_state.master_df = udf
             st.session_state.last_uploaded_file_name = uploaded_excel.name
             st.toast("파일이 안전하게 로드(병합)되었습니다!")
             time.sleep(1)
             st.rerun()

    if st.session_state.master_df is not None:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as writer: st.session_state.master_df.to_excel(writer, index=False)
        st.download_button("💾 엑셀 백업", buf.getvalue(), f"백업_{datetime.now().strftime('%H%M')}.xlsx")
        if st.button("☁️ 클라우드 저장"):
            if save_backup_to_cloud(MODE_KEY, st.session_state.master_df): st.toast("저장 완료!", icon="✅")
    
    st.markdown("---")
    with st.expander("➕ 단어 수동 추가"):
        with st.form("manual_add_form"):
            add_orig = st.text_input("원본 단어")
            add_root = st.text_input("원형")
            add_origin = st.selectbox("분류", ["고", "한", "외", "혼"])
            add_pos = st.selectbox("품사", ["명사", "동사", "형용사", "부사", "관형사", "대명사"]) 
            if st.form_submit_button("추가 및 학습"):
                # [2. 수정 내용 초기화 방지 해결]
                if st.session_state.analysis_source:
                    apply_editor_changes_safe(st.session_state.analysis_source)
                
                if add_orig and add_root and sheet:
                    row = [datetime.now().isoformat(), add_orig, add_root, add_origin, add_pos, 'add', '수동추가']
                    if send_data_with_retry(sheet, row, is_multiple=False):
                        if st.session_state.analysis_result is not None:
                            origin_map = {'고': '🔵 고', '한': '🟢 한', '외': '🔴 외', '혼': '🟣 혼'}
                            pos_map = {'명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사', '부사': '⚡ 부사', '관형사': '🔍 관형사', '대명사': '👤 대명사'}
                            new_item = {
                                'delete_check': False, 'status': '✅ 수동', 'count': '1회',
                                'original_word': add_orig, 'root_word': add_root,
                                'origin': origin_map.get(add_origin, add_origin),
                                'pos': pos_map.get(add_pos, add_pos)
                            }
                            st.session_state.analysis_result.append(new_item)
                        st.toast(f"✅ '{add_orig}' 추가 완료!", icon="🎓")
                        st.rerun()

    # [5. 이력 검색 복원]
    st.markdown("---")
    st.subheader("🔍 이력 검색")
    search_query = st.text_input("궁금한 단어")
    if search_query and sheet_data:
        history = [row for row in sheet_data if search_query in str(row.get('root_word')) or search_query in str(row.get('original_word'))]
        if history:
            for h in history[-3:]:
                st.caption(f"{h['timestamp'][:10]} [{h['action']}] {h['original_word']} -> {h['root_word']}")

# [탭 구성]
tab_file, tab_manual = st.tabs(["📄 파일 분석", "✍️ 직접 입력"])
target_text = ""
run_analysis = False

# [Tab 1: 파일]
with tab_file:
    col_up, col_p = st.columns([3, 1])
    with col_up:
        uploaded_file = st.file_uploader("PDF / 이미지 업로드", type=['pdf', 'png', 'jpg'], key='file_up')
        if uploaded_file and uploaded_file != st.session_state.uploaded_file:
            st.session_state.uploaded_file = uploaded_file
            st.session_state.current_page_idx = 0
            st.session_state.analysis_result = None
            if "pdf" in uploaded_file.type:
                try:
                    if PLUMBER_AVAILABLE: 
                        with pdfplumber.open(uploaded_file) as pdf: st.session_state.total_pages = len(pdf.pages)
                    elif FITZ_AVAILABLE:
                        with fitz.open(stream=uploaded_file.getvalue(), filetype="pdf") as doc: st.session_state.total_pages = len(doc)
                except: pass
            else: st.session_state.total_pages = 1
            st.rerun()

    is_pdf = st.session_state.uploaded_file and "pdf" in st.session_state.uploaded_file.type
    with col_p:
        if is_pdf:
            new_off = st.number_input("시작 쪽수", min_value=1, value=st.session_state.start_page_offset)
            if new_off != st.session_state.start_page_offset:
                st.session_state.start_page_offset = new_off
                st.rerun()

    file_text_val = ""
    if st.session_state.uploaded_file:
        cur_page_lbl = str(st.session_state.current_page_idx + st.session_state.start_page_offset) if is_pdf else st.session_state.manual_page_input
        
        # [3. 페이지 한번에 이동 기능 복원]
        if is_pdf and st.session_state.total_pages > 1:
            c1, c2, c3 = st.columns([1,1,1])
            with c1: 
                if st.button("◀ 이전"): 
                    if st.session_state.current_page_idx > 0:
                        st.session_state.current_page_idx -= 1
                        st.session_state.analysis_result = None
                        st.rerun()
            with c2:
                # 점프 기능 number_input 추가
                target_page = st.number_input("이동", min_value=1, max_value=st.session_state.total_pages, value=st.session_state.current_page_idx + 1, label_visibility="collapsed")
                if target_page != st.session_state.current_page_idx + 1:
                    st.session_state.current_page_idx = target_page - 1
                    st.session_state.analysis_result = None
                    st.rerun()
            with c3:
                if st.button("다음 ▶"):
                    if st.session_state.current_page_idx < st.session_state.total_pages - 1:
                        st.session_state.current_page_idx += 1
                        st.session_state.analysis_result = None
                        st.rerun()
            st.caption(f"{st.session_state.current_page_idx + 1}/{st.session_state.total_pages} (교과서 {cur_page_lbl}쪽)")

        c_img, c_txt = st.columns(2)
        with c_img:
            ibytes = get_page_image_bytes(st.session_state.uploaded_file, st.session_state.current_page_idx)
            if ibytes: st.image(ibytes, use_container_width=True)
        with c_txt:
            with st.spinner("텍스트 추출 중..."):
                extracted = extract_text_unified(st.session_state.uploaded_file, st.session_state.current_page_idx)
            file_text_val = st.text_area("추출된 텍스트", extracted, height=400)
            if st.button("🚀 분석 실행", key='btn_file', type="primary"):
                target_text = file_text_val
                st.session_state.analysis_source = 'FILE'
                run_analysis = True
    
    if st.session_state.analysis_source == 'FILE':
         final_pg_file = str(st.session_state.current_page_idx + st.session_state.start_page_offset) if is_pdf else st.session_state.manual_page_input
         render_analysis_ui(final_pg_file, 'FILE')

# [Tab 2: 직접 입력]
with tab_manual:
    manual_val = st.text_area("분석할 텍스트 입력", height=300)
    pg_val = st.text_input("쪽수", value=st.session_state.manual_page_input)
    if pg_val != st.session_state.manual_page_input: st.session_state.manual_page_input = pg_val
    
    if st.button("🚀 분석 실행", key='btn_manual', type="primary"):
        if manual_val.strip():
            target_text = manual_val
            st.session_state.analysis_source = 'MANUAL'
            run_analysis = True
    
    if st.session_state.analysis_source == 'MANUAL':
        render_analysis_ui(st.session_state.manual_page_input, 'MANUAL')

# [공통 분석 실행]
if run_analysis and target_text:
    with st.spinner("AI 분석 중..."):
        raw_res = get_analysis_hybrid(target_text, sheet_data, MODE_KEY)
        
        if raw_res:
            blacklist = get_blacklist_from_sheet(sheet_data)
            prob_words = get_problematic_words(sheet_data)
            POS_OK = ['명사', '동사', '형용사', '부사', '관형사', '대명사']
            
            filtered = []
            for item in raw_res:
                orig = item.get('original_word', '').replace(" ", "")
                root = item.get('root_word', '')
                pos = item.get('pos', '')
                
                if not pos or pos not in POS_OK: continue
                if orig in blacklist or root in blacklist: continue
                
                if item.get('origin') == '순': item['origin'] = '고'
                item['origin'] = add_emoji_to_origin(item.get('origin', ''))
                pos_map = {'명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사', '부사': '⚡ 부사', '관형사': '🔍 관형사', '대명사': '👤 대명사'}
                item['pos'] = pos_map.get(pos, pos)
                filtered.append(item)
            
            grp = {}
            for it in filtered:
                r = it['root_word']
                if r not in grp: grp[r] = {'root_word': r, 'origin': it['origin'], 'pos': it['pos'], 'originals': []}
                grp[r]['originals'].append(it['original_word'])
            
            final = []
            for r, info in grp.items():
                cnts = Counter(info['originals'])
                orig_str = ", ".join([f"{k}({v})" for k,v in cnts.items()])
                tot = sum(cnts.values())
                trusted = check_trust_level_strict(r, st.session_state.master_df, prob_words)
                final.append({
                    'delete_check': False,
                    'status': '✅ 자동' if trusted else '📝 검토',
                    'count': f"{tot}회",
                    'original_word': orig_str,
                    'root_word': r,
                    'origin': info['origin'],
                    'pos': info['pos']
                })
            st.session_state.analysis_result = final
            st.rerun() 
        else:
            st.session_state.analysis_result = None
            st.warning("분석 결과가 없습니다.")

if st.session_state.master_df is not None and not st.session_state.master_df.empty:
    st.markdown("---")
    with st.expander("📊 현재까지 모인 데이터 통계"):
        stat_df = st.session_state.master_df
        c1, c2 = st.columns(2)
        with c1:
            st.metric("총 단어 수", len(stat_df))
            if '구분' in stat_df.columns:
                st.bar_chart(stat_df['구분'].value_counts())
        with c2:
            if '출연횟수' in stat_df.columns:
                st.write("**최다 빈도 TOP 10**")
                st.dataframe(stat_df.sort_values('출연횟수', ascending=False).head(10)[['구분','자료','출연횟수']], hide_index=True)