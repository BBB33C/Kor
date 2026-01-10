import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import os
import gspread
import base64
import traceback  # 에러 추적용
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
# ⚙️ 설정
# =========================================================
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        # 여기에 본인의 API KEY를 입력하세요
        API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk" 
except:
    API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk"

MODEL_NAME = "gemini-2.0-flash-exp"
SHEET_NAME = "Korean_DB" 
TRUST_THRESHOLD = 3 

st.set_page_config(page_title="국어활동 AI 분석기(DEBUG)", page_icon="🐞", layout="wide")

# =========================================================
# 🔐 구글 시트 연결
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
            st.error(f"❌ '{target_sheet_name}' 시트를 찾을 수 없습니다.")
            return None, []
        data = sheet.get_all_records()
        return sheet, data
    except Exception as e:
        st.error(f"구글 시트 연결 오류: {e}")
        return None, []

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
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            else:
                st.error(f"❌ 데이터 전송 실패: {str(e)}")
                return False
    return False

# [백업 기능]
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
        
        data_to_upload = [df.columns.values.tolist()] + df.astype(str).values.tolist()
        worksheet.update(data_to_upload)
        return True
    except Exception as e:
        print(f"자동 백업 실패: {e}") 
        return False

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
# 🧠 AI 및 전처리 로직 (디버깅 강화)
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
        if not added.empty:
            missing_counts = added['original_word'].value_counts().head(10)
            for word, _ in missing_counts.items():
                prompt_lines.append(f"- [학습된 필수]: '{word}'는 무조건 포함하세요.")
        modified = df[df['action'] == 'modify']
        for _, row in modified.tail(15).iterrows():
             prompt_lines.append(f"- [학습된 수정]: '{row['original_word']}'가 나오면 무조건 원형:'{row['root_word']}', 분류:'{row['origin']}'로 처리하세요.")
             
    if prompt_lines:
         return "\n[🚨 최우선 사용자 학습 규칙 (이것이 법이다)]:\n" + "\n".join(prompt_lines) + "\n"
    return ""

def api_call_direct(prompt):
    """
    [디버깅 수정] 에러 발생 시 상세 정보를 화면에 출력하도록 수정됨
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    data = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}}
    
    try:
        with st.expander("📡 API 전송 데이터 확인 (개발자용)", expanded=False):
            st.code(prompt[:500] + "...", language="text") # 프롬프트 앞부분 확인

        response = requests.post(url, headers=headers, json=data, timeout=300)
        
        # [DEBUG] 상태 코드 확인
        if response.status_code != 200:
            st.error(f"❌ API 호출 실패! 상태 코드: {response.status_code}")
            st.error(f"에러 메시지: {response.text}")
            return None
            
        result_json = response.json()
        
        # [DEBUG] 응답 구조 확인
        if 'candidates' not in result_json:
            st.error("❌ API 응답에 'candidates'가 없습니다. (필터링되었을 가능성 있음)")
            st.json(result_json) # 전체 응답 출력
            return None
            
        text_res = result_json['candidates'][0]['content']['parts'][0]['text']
        
        # [DEBUG] 원본 텍스트 확인
        # st.text_area("🤖 AI 원본 응답", text_res, height=100)
        
        json_match = re.search(r'\[.*\]', text_res, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        else:
            st.error("❌ AI가 JSON 형식을 반환하지 않았습니다.")
            st.text(f"받은 내용: {text_res}")
            return None
            
    except Exception as e:
        st.error("❌ 서버 통신 중 예외 발생")
        st.code(traceback.format_exc()) # 상세 에러 로그 출력
        return None

def api_call_vision_ocr(image_bytes):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    prompt_text = "이 이미지의 텍스트를 추출하세요. 줄바꿈을 지키세요."
    data = {"contents": [{"parts": [{"text": prompt_text}, {"inline_data": {"mime_type": "image/png", "data": base64_image}}]}], "generationConfig": {"temperature": 0.1}}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code != 200: return f"OCR 실패: {response.text}"
        result_json = response.json()
        if 'candidates' in result_json: return result_json['candidates'][0]['content']['parts'][0]['text']
        return "텍스트를 찾을 수 없습니다."
    except Exception as e: return f"OCR 통신 오류: {e}"

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
        except Exception as e: return f"이미지 읽기 오류: {e}"
    elif "pdf" in file_type:
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(file_obj) as pdf:
                    if page_index < 0 or page_index >= len(pdf.pages): return ""
                    page = pdf.pages[page_index]
                    width, height = page.width, page.height
                    crop_box = (0, 0, width, height * 0.9)
                    try: cropped = page.crop(crop_box); text = cropped.extract_text()
                    except: text = page.extract_text()
                    if text and len(text.strip()) > 30: return text
            except: pass
        if FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_index < 0 or page_index >= len(doc): return ""
                page = doc[page_index]
                rect = page.rect
                clip_rect = fitz.Rect(0, 0, rect.width, rect.height * 0.9)
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip_rect)
                return api_call_vision_ocr(pix.tobytes("png"))
            except Exception as e: return f"PDF 변환 오류: {e}"
        return "PDF를 읽을 수 없습니다."
    return "지원하지 않는 파일 형식입니다."

def get_page_image_bytes(file_obj, page_index):
    file_type = file_obj.type
    if "image" in file_type:
        return file_obj.getvalue() 
    elif "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
            if page_index < 0 or page_index >= len(doc): return None
            page = doc[page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)) 
            return pix.tobytes("png") 
        except: return None
    return None

def get_analysis_hybrid(text, sheet_data, mode_key):
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    role_definition = """
    당신은 국어학 전문가입니다.
    """
    
    mode_instruction = "두음법칙 미적용" if mode_key == "NORTH" else "표준어법 준수"

    base_instruction = f"""
    {role_definition}
    {mode_instruction}
    
    [작성 규칙]
    1. 명사+하다 -> 명사만 추출 (공부하다 -> 공부, 명사)
    2. 조사/어미/문장부호 제외.
    3. 결과는 오직 JSON 리스트로만 출력.
    
    형식: [{{"original_word": "...", "root_word": "...", "origin": "고", "pos": "명사"}}]
    """
    
    chunks = split_text_smartly(text)
    all_results = []
    
    # [DEBUG] 청크 정보 출력
    st.info(f"ℹ️ 텍스트를 {len(chunks)}개 덩어리로 나누어 분석합니다.")
    
    for i, chunk in enumerate(chunks):
        if not chunk.strip(): continue
        prompt = f"""{learning_prompt}\n{base_instruction}\n\n분석할 문장:\n"{chunk}" """
        
        with st.spinner(f"⏳ {i+1}번째 덩어리 분석 중... ({len(chunk)}자)"):
            chunk_result = api_call_direct(prompt)
            
        if chunk_result: 
            all_results.extend(chunk_result)
        else:
            st.warning(f"⚠️ {i+1}번째 덩어리 분석 실패 (결과 없음)")
            
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

def apply_editor_changes():
    if "editor" in st.session_state and st.session_state.analysis_result:
        changes = st.session_state["editor"].get("edited_rows", {})
        for idx, changes_dict in changes.items():
            if idx < len(st.session_state.analysis_result):
                for col, val in changes_dict.items():
                    st.session_state.analysis_result[idx][col] = val

# =========================================================
# 🖥️ 메인 화면 로직
# =========================================================
st.title("🐞 국어활동 AI 분석기 (DEBUG MODE)")
st.caption("🚨 현재 디버그 모드입니다. 에러 발생 시 상세 정보가 표시됩니다.")

with st.sidebar:
    st.header("🏳️ 분석 모드 선택")
    mode_selection = st.radio("분석할 언어 환경", ("🇰🇷 대한민국 표준어", "🇰🇵 북한 문화어"))
    MODE_KEY = "SOUTH" if "대한민국" in mode_selection else "NORTH"
    
    if 'last_mode' not in st.session_state: st.session_state.last_mode = MODE_KEY
    if st.session_state.last_mode != MODE_KEY:
        st.session_state.analysis_result = None
        if st.session_state.master_df is not None and not st.session_state.master_df.empty:
            prev_mode = st.session_state.last_mode
            save_backup_to_cloud(prev_mode, st.session_state.master_df)
        st.session_state.master_df = None 
        st.session_state.last_uploaded_file_name = None 
        st.session_state.last_mode = MODE_KEY
        st.rerun()

    connected_tab_name = 'South_Korea' if MODE_KEY=='SOUTH' else 'North_Korea'
    st.success(f"현재 **[{mode_selection}]** 모드")
    st.markdown("---")

sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)

if 'analysis_result' not in st.session_state: st.session_state.analysis_result = None
if 'excel_buffer' not in st.session_state: st.session_state.excel_buffer = None
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'last_uploaded_file_name' not in st.session_state: st.session_state.last_uploaded_file_name = None
if 'analysis_source' not in st.session_state: st.session_state.analysis_source = None 

if 'uploaded_file' not in st.session_state: st.session_state.uploaded_file = None
if 'total_pages' not in st.session_state: st.session_state.total_pages = 0
if 'current_page_idx' not in st.session_state: st.session_state.current_page_idx = 0
if 'start_page_offset' not in st.session_state: st.session_state.start_page_offset = 1 
if 'manual_page_input' not in st.session_state: st.session_state.manual_page_input = "1" 

uploaded_df = None
with st.sidebar:
    st.header("📂 이어하기 & 백업")
    uploaded_excel = st.file_uploader("작업하던 엑셀 파일", type=['xlsx'])
    
    if uploaded_excel:
        if uploaded_excel.name != st.session_state.last_uploaded_file_name:
            uploaded_df = load_excel_safely(uploaded_excel)
            if uploaded_df is not None:
                 if st.session_state.master_df is not None and not st.session_state.master_df.empty:
                     merged_df = pd.concat([st.session_state.master_df, uploaded_df], ignore_index=True)
                     st.session_state.master_df = merged_df.drop_duplicates(subset=['자료', '구분'], keep='first').reset_index(drop=True)
                 else:
                     st.session_state.master_df = uploaded_df.copy()
                 st.session_state.last_uploaded_file_name = uploaded_excel.name
                 st.rerun() 

    if st.session_state.master_df is not None and not st.session_state.master_df.empty:
        backup_buffer = io.BytesIO()
        with pd.ExcelWriter(backup_buffer, engine='openpyxl') as writer: st.session_state.master_df.to_excel(writer, index=False)
        backup_buffer.seek(0)
        st.download_button("💾 PC에 엑셀 백업", backup_buffer, f"백업_{datetime.now().strftime('%H%M')}.xlsx")
        
        if st.button("☁️ 클라우드 저장"):
            if save_backup_to_cloud(MODE_KEY, st.session_state.master_df): st.success("저장 완료")
            else: st.error("저장 실패")

    if (st.session_state.master_df is None or st.session_state.master_df.empty) and sheet:
        if st.button("📂 클라우드 불러오기"):
            restored_df = load_backup_from_cloud(MODE_KEY)
            if restored_df is not None and not restored_df.empty:
                st.session_state.master_df = restored_df
                st.rerun()

    st.markdown("---")
    
    with st.expander("➕ 단어 수동 추가"):
        with st.form("manual_add_form"):
            add_orig = st.text_input("원본 단어")
            add_root = st.text_input("원형")
            add_origin = st.selectbox("분류", ["고", "한", "외", "혼"])
            add_pos = st.selectbox("품사", ["명사", "동사", "형용사", "부사", "관형사", "대명사"]) 
            
            if st.form_submit_button("추가 및 학습"):
                apply_editor_changes()
                if add_orig and add_root and sheet:
                    row = [datetime.now().isoformat(), add_orig, add_root, add_origin, add_pos, 'add', '수동추가']
                    if send_data_with_retry(sheet, row):
                        st.toast(f"✅ 추가 완료!", icon="🎓")

# [UI] 탭 구성
st.subheader("🧐 분석 대상 입력")
tab_file, tab_manual = st.tabs(["📄 파일 분석", "✍️ 직접 텍스트 입력"])

target_text_for_analysis = "" 
run_analysis_flag = False

# [Tab 1: 파일]
with tab_file:
    col_up, col_set = st.columns([3, 1])
    with col_up:
        uploaded_file = st.file_uploader("파일 업로드 (PDF/IMG)", type=['pdf', 'png', 'jpg'], key='file_uploader')
        
        if uploaded_file and uploaded_file != st.session_state.uploaded_file:
            st.session_state.uploaded_file = uploaded_file
            st.session_state.current_page_idx = 0
            st.session_state.analysis_result = None
            
            file_type = uploaded_file.type
            if "pdf" in file_type:
                try:
                    if PLUMBER_AVAILABLE:
                        with pdfplumber.open(uploaded_file) as pdf: st.session_state.total_pages = len(pdf.pages)
                    elif FITZ_AVAILABLE:
                        doc = fitz.open(stream=uploaded_file.getvalue(), filetype="pdf")
                        st.session_state.total_pages = len(doc)
                except: pass
            else:
                st.session_state.total_pages = 1
            st.rerun()

    is_pdf_mode = st.session_state.uploaded_file and "pdf" in st.session_state.uploaded_file.type

    with col_set:
        if is_pdf_mode:
            st.write("") 
            new_offset = st.number_input("시작 쪽수", min_value=1, value=st.session_state.start_page_offset)
            if new_offset != st.session_state.start_page_offset:
                st.session_state.start_page_offset = new_offset
                st.rerun()

    file_extracted_text = "" 
    
    if st.session_state.uploaded_file:
        current_save_page = str(st.session_state.current_page_idx + st.session_state.start_page_offset) if is_pdf_mode else st.session_state.manual_page_input

        if is_pdf_mode and st.session_state.total_pages > 1:
            c_prev, c_jump, c_next = st.columns([1, 1, 1])
            with c_prev:
                if st.button("◀ 이전"):
                    if st.session_state.current_page_idx > 0:
                        st.session_state.current_page_idx -= 1
                        st.session_state.analysis_result = None
                        st.rerun()
            with c_next:
                if st.button("다음 ▶"):
                    if st.session_state.current_page_idx < st.session_state.total_pages - 1:
                        st.session_state.current_page_idx += 1
                        st.session_state.analysis_result = None
                        st.rerun()
            st.caption(f"{st.session_state.current_page_idx + 1} / {st.session_state.total_pages} (교과서 {current_save_page}쪽)")

        col_view1, col_view2 = st.columns(2)
        with col_view1:
            img_bytes = get_page_image_bytes(st.session_state.uploaded_file, st.session_state.current_page_idx)
            if img_bytes: st.image(img_bytes, caption="미리보기")

        with col_view2:
            extracted_text = extract_text_unified(st.session_state.uploaded_file, st.session_state.current_page_idx)
            file_extracted_text = st.text_area("파일 추출 텍스트", value=extracted_text, height=400)
            
            if st.button("🚀 파일 내용 분석", type="primary"):
                target_text_for_analysis = file_extracted_text 
                run_analysis_flag = True
                st.session_state.analysis_source = 'FILE'

# [Tab 2: 수동]
with tab_manual:
    manual_text_input = st.text_area("분석 대상 텍스트 직접 입력", height=400, placeholder="여기에 텍스트를 입력하세요.")
    manual_page_val_2 = st.text_input("쪽수 입력", value=st.session_state.manual_page_input)
    if manual_page_val_2 != st.session_state.manual_page_input:
        st.session_state.manual_page_input = manual_page_val_2
            
    if st.button("🚀 입력 텍스트 분석", type="primary", key="btn_manual"):
        if manual_text_input.strip():
            target_text_for_analysis = manual_text_input 
            run_analysis_flag = True
            st.session_state.analysis_source = 'MANUAL'
        else:
            st.warning("⚠️ 텍스트가 비어있습니다!")

# [분석 실행 및 디버깅]
if run_analysis_flag:
    st.divider()
    st.subheader("🛠️ 디버깅 정보 (Internal Logs)")
    
    # 1. 입력값 확인
    if not target_text_for_analysis:
        st.error("❌ 분석할 텍스트(target_text_for_analysis)가 비어있습니다.")
    else:
        st.success(f"✅ 입력 확인: {len(target_text_for_analysis)}글자")
        with st.expander("입력된 텍스트 확인"):
            st.text(target_text_for_analysis)
        
        # 2. 분석 함수 실행
        raw_results = get_analysis_hybrid(target_text_for_analysis, sheet_data, MODE_KEY)
        
        # 3. 결과 수신 확인
        if raw_results is None:
            st.error("❌ get_analysis_hybrid 함수가 None을 반환했습니다. (API 오류 추정)")
        elif len(raw_results) == 0:
            st.warning("⚠️ 분석 결과 리스트가 비어있습니다. (AI가 단어를 하나도 못 찾음)")
        else:
            st.success(f"✅ {len(raw_results)}개의 데이터를 수신했습니다.")
            
            # 필터링 로직
            validation_text = target_text_for_analysis.replace(" ", "")
            blacklist = get_blacklist_from_sheet(sheet_data)
            
            pre_filtered_items = []
            for item in raw_results:
                original = item.get('original_word', '').replace(" ", "")
                root = item.get('root_word', '')
                
                # [DEBUG] 단어별 통과 여부 로그
                # st.write(f"검토: {original} -> {root}")
                
                if original in blacklist or root in blacklist: continue
                
                item['origin'] = add_emoji_to_origin(item.get('origin', ''))
                pos_map = {'명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사', '부사': '⚡ 부사', '관형사': '🔍 관형사', '대명사': '👤 대명사'}
                item['pos'] = pos_map.get(item.get('pos'), item.get('pos'))
                pre_filtered_items.append(item)
            
            # 결과 가공
            grouped_data = {} 
            for item in pre_filtered_items:
                root = item['root_word']
                if root not in grouped_data:
                    grouped_data[root] = {'root_word': root, 'origin': item['origin'], 'pos': item['pos'], 'originals': []}
                grouped_data[root]['originals'].append(item['original_word'])

            final_results = []
            for root, info in grouped_data.items():
                orig_counts = Counter(info['originals'])
                formatted_original = ", ".join([f"{word}({cnt})" for word, cnt in orig_counts.items()])
                total_cnt = sum(orig_counts.values()) 
                is_trusted = check_trust_level_strict(root, st.session_state.master_df, get_problematic_words(sheet_data))
                status = '✅ 자동' if is_trusted else '📝 검토'
                final_results.append({'delete_check': False, 'status': status, 'count': f"{total_cnt}회", 'original_word': formatted_original, 'root_word': root, 'origin': info['origin'], 'pos': info['pos']})
            
            st.session_state.analysis_result = final_results

# [결과 표시]
if st.session_state.analysis_result:
    st.markdown("---")
    st.markdown("### 📊 분석 결과 테이블")
    
    df_display = pd.DataFrame(st.session_state.analysis_result)
    
    column_config = {
        "delete_check": st.column_config.CheckboxColumn("삭제", width="small"),
        "count": st.column_config.TextColumn("빈도"), 
        "original_word": st.column_config.TextColumn("원본"), 
        "root_word": "원형",
        "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
        "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사"])
    }
    cols = ["delete_check", "count", "original_word", "root_word", "origin", "pos"]
    
    edited_df = st.data_editor(df_display[cols], column_config=column_config, use_container_width=True, num_rows="fixed", key="editor")
    
    if st.button("💾 결과 저장"):
        st.success("저장 로직은 생략되었습니다 (디버깅 집중).")