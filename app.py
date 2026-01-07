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
# ⚙️ 설정
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

# [백업 기능] 클라우드 백업 (모드별 시트 분리)
def save_backup_to_cloud(mode_key, df):
    client = get_google_sheet_client()
    if not client or df is None or df.empty: return False
    
    # [핵심] 모드에 따라 백업 시트 이름을 다르게 설정
    backup_sheet_name = f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}"
    
    try:
        spreadsheet = client.open(SHEET_NAME)
        try:
            worksheet = spreadsheet.worksheet(backup_sheet_name)
            worksheet.clear() # 기존 백업 삭제 (덮어쓰기)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=backup_sheet_name, rows=1000, cols=20)
        
        # DataFrame을 리스트로 변환하여 업로드
        data_to_upload = [df.columns.values.tolist()] + df.astype(str).values.tolist()
        worksheet.update(data_to_upload)
        return True
    except Exception as e:
        print(f"자동 백업 실패: {e}") 
        return False

# [백업 기능] 클라우드 복구 (모드별 시트 분리)
def load_backup_from_cloud(mode_key):
    client = get_google_sheet_client()
    if not client: return None
    
    # [핵심] 현재 모드에 맞는 백업 시트만 로드
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
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    data = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code != 200:
            st.error(f"❌ AI 응답 실패 (코드 {response.status_code}): {response.text}")
            return None
        result_json = response.json()
        if 'candidates' in result_json:
            text_res = result_json['candidates'][0]['content']['parts'][0]['text']
            json_match = re.search(r'\[.*\]', text_res, re.DOTALL)
            if json_match: return json.loads(json_match.group())
        return None
    except Exception as e:
        st.error(f"❌ 서버 통신 오류: {e}")
        return None

def api_call_vision_ocr(image_bytes):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    prompt_text = """
    이 이미지에 있는 텍스트를 보이는 그대로 추출해주세요.
    
    [중요한 형식 규칙]
    1. **공간 분리 준수:** 말풍선, 단락, 표 등으로 시각적으로 분리된 텍스트 덩어리는 반드시 **줄바꿈(Enter)**으로 명확히 구분하세요.
    2. **세로쓰기 대응:** 글자가 세로로(위에서 아래로) 쓰여 있다면, 자연스러운 독해 순서(우측 상단 -> 좌측 하단)를 따르세요.
    3. **북한 표기 유지:** 두음법칙을 적용하지 않은 표기(예: 로동, 녀자)는 수정하지 말고 그대로 적으세요.
    4. **중복 포함(필수):** 같은 단어나 문장이 여러 번 나오면 합치지 말고 **나온 횟수만큼 반복해서** 적으세요. (빈도수 분석용)
    5. **노이즈 제거:** 쪽수, 머리말은 제외하세요.
    """
    
    data = {"contents": [{"parts": [{"text": prompt_text}, {"inline_data": {"mime_type": "image/png", "data": base64_image}}]}], "generationConfig": {"temperature": 0.1}}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code != 200: return f"OCR 실패: {response.text}"
        result_json = response.json()
        if 'candidates' in result_json: return result_json['candidates'][0]['content']['parts'][0]['text']
        return "텍스트를 찾을 수 없습니다."
    except Exception as e: return f"OCR 통신 오류: {e}"

try:
    from konlpy.tag import Okt
    MORPHOLOGY_AVAILABLE = True
except:
    MORPHOLOGY_AVAILABLE = False

def preprocess_with_morphology(text):
    if not MORPHOLOGY_AVAILABLE: return None
    try:
        okt = Okt()
        pos = okt.pos(text, stem=True)
        return [w for w, p in pos if p in ['Noun', 'Verb', 'Adjective', 'Adverb', 'Determiner'] and len(w) > 1]
    except: return None

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
        return "PDF를 읽을 수 없습니다. (라이브러리 설치 확인 필요)"
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
    당신은 국립국어원 표준국어대사전 편찬에 참여하는 '국어학 및 어원 분석 전문가'입니다.
    주어진 텍스트에서 '실질 형태소(알맹이 단어)'를 분석하고, 그 단어의 어원(Origin)을 국어사전 기준으로 엄격하게 판별하세요.
    """
    
    if mode_key == "NORTH":
        mode_instruction = """
        [🇰🇵 북한 문화어 분석 모드]
        - 당신은 '북한 문화어(Munhwa-o)' 전문가입니다.
        - **두음법칙을 적용하지 마세요.** (예: '노동'이 아니라 '로동')
        - 북한 특유의 어휘나 표기법이 있다면 이를 존중하여 원형을 추출하세요.
        """
    else:
        mode_instruction = """
        [🇰🇷 대한민국 표준어 분석 모드]
        - 당신은 '대한민국 표준어' 전문가입니다.
        - 국립국어원 표준 맞춤법과 두음법칙을 준수하세요.
        """

    base_instruction = f"""
    {role_definition}
    {mode_instruction}
    
    [핵심 작성 규칙]
    - original_word: 문장에서 **실제로 쓰인 형태 그대로(활용형 포함)** 적으세요.
    - root_word: 사전에 등재된 **기본형(원형)**으로 적으세요.
    
    [어원 분류 기준 (매우 중요)]
    - **고(고유어):** 순우리말 (예: 하늘, 아버지, 바람)
    - **한(한자어):** 한자에 뿌리를 둔 말 (예: 학교, 학생, 귤, 점심)
    - **외(외래어):** 외국에서 들어와 우리말처럼 쓰이는 말 (예: 버스, 컴퓨터, 가방, 빵, 담배, 냄비)
    - **혼(혼종어):** 서로 다른 어종이 결합된 말 (예: 비빔밥, 가지각색)

    [동음이의어 구분 규칙]
    - **사람 이름(인명)**: 원형 뒤에 **(이름)** 붙이기 (예: 철수(이름))
    - **지명(장소)**: 원형 뒤에 **(지명)** 붙이기 (예: 평양(지명))
    - 그 외 동음이의어는 괄호로 뜻 구분
    
    [기본 규칙]
    - 조사, 어미, 접사, 문장부호, 감탄사 제외
    - 품사: '명사', '동사', '형용사', '부사', '관형사'
    - **[중요] 같은 단어가 여러 번 나오면 합치지 말고 나온 횟수만큼 JSON 객체를 반복해서 만드세요.**
    
    형식: [{{"original_word": "...", "root_word": "...", "origin": "고", "pos": "명사"}}]
    """
    chunks = split_text_smartly(text)
    all_results = []
    for i, chunk in enumerate(chunks):
        if not chunk.strip(): continue
        keywords = preprocess_with_morphology(chunk)
        if keywords: prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{chunk}"\n힌트: {', '.join(keywords)}"""
        else: prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{chunk}" """
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
        return val.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('❗ ', '').replace('✅ ', '').replace('📝 ', '')
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

# =========================================================
# 🖥️ 메인 화면 로직
# =========================================================
st.title("📝 국어활동 AI 분석기")

with st.sidebar:
    st.header("🏳️ 분석 모드 선택")
    mode_selection = st.radio("분석할 언어 환경", ("🇰🇷 대한민국 표준어", "🇰🇵 북한 문화어"))
    MODE_KEY = "SOUTH" if "대한민국" in mode_selection else "NORTH"
    
    if 'last_mode' not in st.session_state: st.session_state.last_mode = MODE_KEY
    if st.session_state.last_mode != MODE_KEY:
        st.session_state.analysis_result = None
        # [핵심 수정] 모드 변경 시 기존 데이터 메모리 초기화 (데이터 섞임 방지)
        st.session_state.master_df = None 
        st.session_state.last_mode = MODE_KEY
        st.rerun()

    connected_tab_name = 'South_Korea' if MODE_KEY=='SOUTH' else 'North_Korea'
    st.success(f"현재 **[{mode_selection}]** 모드입니다.\n\n학습 데이터가 **'{connected_tab_name}'** 탭에 저장됩니다.")
    st.markdown("---")

sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)

if 'analysis_result' not in st.session_state: st.session_state.analysis_result = None
if 'excel_buffer' not in st.session_state: st.session_state.excel_buffer = None
if 'master_df' not in st.session_state: st.session_state.master_df = None

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
        uploaded_df = load_excel_safely(uploaded_excel)
        if uploaded_df is not None:
             st.success(f"📂 파일 로드됨: {len(uploaded_df)}개 단어")
             if st.session_state.master_df is None:
                 st.session_state.master_df = uploaded_df.copy()
        else: st.caption("ℹ️ 빈 파일 혹은 양식이 다릅니다.")
    else:
        if st.session_state.master_df is None:
            st.session_state.master_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])
            
    # [백업 버튼 모음]
    if st.session_state.master_df is not None and not st.session_state.master_df.empty:
        # 1. 파일 다운로드 백업
        backup_buffer = io.BytesIO()
        with pd.ExcelWriter(backup_buffer, engine='openpyxl') as writer: st.session_state.master_df.to_excel(writer, index=False)
        backup_buffer.seek(0)
        st.download_button(
            label="💾 PC에 엑셀 백업",
            data=backup_buffer,
            file_name=f"국어활동_백업_{datetime.now().strftime('%H%M%S')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
        
        # 2. 클라우드 백업 (수동)
        if st.button("☁️ 구글 시트에 임시저장", use_container_width=True):
            with st.spinner("클라우드에 백업 중..."):
                if save_backup_to_cloud(MODE_KEY, st.session_state.master_df):
                    st.success("✅ 클라우드 저장 완료!")
                else:
                    st.error("❌ 저장 실패")

    # [클라우드 복구 버튼]
    if (st.session_state.master_df is None or st.session_state.master_df.empty) and sheet:
        if st.button("📂 클라우드 백업 불러오기", use_container_width=True):
            with st.spinner("백업 찾는 중..."):
                restored_df = load_backup_from_cloud(MODE_KEY)
                if restored_df is not None and not restored_df.empty:
                    st.session_state.master_df = restored_df
                    st.success(f"✅ 복구 완료! ({len(restored_df)}개 단어)")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.warning("⚠️ 저장된 백업 데이터가 없습니다.")

    if sheet: st.caption(f"🌏 지능 연결됨: {len(sheet_data)}건 학습됨")
    else: st.error("❌ 학습 서버 연결 실패")
    
    st.markdown("---")
    
    with st.expander("➕ AI가 놓친 단어 추가하기"):
        with st.form("manual_add_form"):
            add_orig = st.text_input("원본 단어")
            add_root = st.text_input("원형")
            add_origin = st.selectbox("분류", ["고", "한", "외", "혼"])
            add_pos = st.selectbox("품사", ["명사", "동사", "형용사", "부사", "관형사"]) 
            if st.form_submit_button("추가 및 학습"):
                if add_orig and add_root and sheet:
                    row = [datetime.now().isoformat(), add_orig, add_root, add_origin, add_pos, 'add', '수동추가']
                    if send_data_with_retry(sheet, row, is_multiple=False):
                        if st.session_state.analysis_result is not None:
                            origin_map = {'고': '🔵 고', '한': '🟢 한', '외': '🔴 외', '혼': '🟣 혼'}
                            pos_map = {'명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사', '부사': '⚡ 부사', '관형사': '🔍 관형사'}
                            mapped_origin = origin_map.get(add_origin, add_origin)
                            mapped_pos = pos_map.get(add_pos, add_pos)
                            
                            new_item = {
                                'delete_check': False,
                                'status': '✅ 수동',
                                'count': '1회',
                                'original_word': add_orig,
                                'root_word': add_root,
                                'origin': mapped_origin,
                                'pos': mapped_pos
                            }
                            st.session_state.analysis_result.append(new_item)
                            
                        st.toast(f"✅ 추가 완료!", icon="🎓")
                        st.rerun()

    st.markdown("---")
    st.subheader("🔍 이력 검색")
    search_query = st.text_input("궁금한 단어")
    if search_query and sheet_data:
        history = [row for row in sheet_data if search_query in str(row.get('root_word')) or search_query in str(row.get('original_word'))]
        if history:
            for h in history[-3:]:
                st.caption(f"{h['timestamp'][:10]} [{h['action']}] {h['original_word']} -> {h['root_word']}")

# [상단 UI] 파일 업로드
st.subheader("📄 파일 분석 (PDF 또는 이미지)")

col_up, col_set = st.columns([3, 1])
with col_up:
    uploaded_file = st.file_uploader(
        "교과서 파일 업로드", 
        type=['pdf', 'png', 'jpg', 'jpeg'], 
        key='file_uploader'
    )
    
    if uploaded_file and uploaded_file != st.session_state.uploaded_file:
        st.session_state.uploaded_file = uploaded_file
        st.session_state.current_page_idx = 0
        st.session_state.analysis_result = None
        st.session_state.start_page_offset = 1 
        
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
        new_offset = st.number_input("교과서 시작 쪽수", min_value=1, value=st.session_state.start_page_offset, help="PDF의 첫 번째 장이 실제 교과서 몇 쪽인지 설정하세요.")
        if new_offset != st.session_state.start_page_offset:
            st.session_state.start_page_offset = new_offset
            st.rerun()

# [메인 UI] 좌우 분할 뷰
extracted_text = ""

if st.session_state.uploaded_file:
    if is_pdf_mode:
        current_save_page = str(st.session_state.current_page_idx + st.session_state.start_page_offset)
    else:
        current_save_page = st.session_state.manual_page_input

    # 네비게이션
    if is_pdf_mode and st.session_state.total_pages > 1:
        c_prev, c_info, c_next = st.columns([1, 2, 1])
        with c_prev:
            if st.button("◀ 이전 장"):
                if st.session_state.current_page_idx > 0:
                    st.session_state.current_page_idx -= 1
                    st.session_state.analysis_result = None
                    st.rerun()
        with c_next:
            if st.button("다음 장 ▶"):
                if st.session_state.current_page_idx < st.session_state.total_pages - 1:
                    st.session_state.current_page_idx += 1
                    st.session_state.analysis_result = None
                    st.rerun()
        with c_info:
            st.markdown(f"<div style='text-align:center; padding-top:10px;'><b>PDF {st.session_state.current_page_idx + 1}/{st.session_state.total_pages}장 (현재 {current_save_page}쪽)</b></div>", unsafe_allow_html=True)

    view_col1, view_col2 = st.columns([1, 1])
    
    # 스크롤 뷰어
    with view_col1:
        st.caption("📷 원본 미리보기 (휠로 스크롤 가능)")
        img_bytes = get_page_image_bytes(st.session_state.uploaded_file, st.session_state.current_page_idx)
        if img_bytes:
            b64_img = base64.b64encode(img_bytes).decode('utf-8')
            html_code = f"""
            <div style="height: 600px; overflow-y: auto; border: 1px solid #ddd; border-radius: 5px; padding: 10px; background-color: #f9f9f9;">
                <img src="data:image/png;base64,{b64_img}" style="width: 100%; display: block;">
            </div>
            """
            st.markdown(html_code, unsafe_allow_html=True)
        else:
            st.info("미리보기를 불러올 수 없습니다.")

    with view_col2:
        st.caption("📝 추출 텍스트 (수정 가능)")
        with st.spinner("텍스트 읽는 중..."):
            extracted_text = extract_text_unified(st.session_state.uploaded_file, st.session_state.current_page_idx)
            if "오류" in extracted_text: st.error(extracted_text)
            
        input_text = st.text_area(
            "분석 대상", 
            value=extracted_text if extracted_text else "",
            height=600,
            label_visibility="collapsed"
        )
        
        col_p, col_b = st.columns([1, 2])
        with col_p:
            if not is_pdf_mode: 
                manual_page = st.text_input("저장될 쪽수 입력", value=st.session_state.manual_page_input, key="manual_page_setter")
                if manual_page != st.session_state.manual_page_input:
                    st.session_state.manual_page_input = manual_page
            else:
                st.info(f"💾 **{current_save_page}쪽**으로 저장됩니다.")

        with col_b:
            analyze_btn = st.button("🚀 분석 실행", use_container_width=True, type="primary")

else:
    st.info("👆 위에서 파일을 업로드해주세요.")
    analyze_btn = False
    input_text = ""

# 분석 및 결과 처리
if analyze_btn and input_text:
    with st.spinner(f"{mode_selection} 모드로 분석 중입니다..."):
        raw_results = get_analysis_hybrid(input_text, sheet_data, MODE_KEY)
        if raw_results:
            validation_text = input_text.replace(" ", "")
            POS_WHITELIST = ['명사', '동사', '형용사', '부사', '관형사'] 
            blacklist = get_blacklist_from_sheet(sheet_data)
            problematic_words = get_problematic_words(sheet_data)
            
            pre_filtered_items = []
            for item in raw_results:
                original = item.get('original_word', '').replace(" ", "")
                root = item.get('root_word', '')
                pos = item.get('pos', '')
                orig_check = original.split('(')[0]
                if orig_check not in validation_text: pass 
                if not pos or pos not in POS_WHITELIST: continue
                if original in blacklist or root in blacklist: continue
                if item.get('origin') == '순': item['origin'] = '고'
                item['origin'] = add_emoji_to_origin(item.get('origin', ''))
                pos_map = {'명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사', '부사': '⚡ 부사', '관형사': '🔍 관형사'}
                item['pos'] = pos_map.get(pos, pos)
                pre_filtered_items.append(item)
            
            # 정확도 향상을 위한 그룹화 로직
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
                is_trusted = check_trust_level_strict(root, st.session_state.master_df, problematic_words)
                status = '✅ 자동' if is_trusted else '📝 검토'
                final_results.append({'delete_check': False, 'status': status, 'count': f"{total_cnt}회", 'original_word': formatted_original, 'root_word': root, 'origin': info['origin'], 'pos': info['pos']})
            
            st.session_state.analysis_result = final_results

if st.session_state.analysis_result:
    st.markdown("---")
    st.markdown("### 📊 분석 결과")
    
    df_display = pd.DataFrame(st.session_state.analysis_result)
    
    column_config = {
        "delete_check": st.column_config.CheckboxColumn("삭제", width="small"),
        "count": st.column_config.TextColumn("빈도", disabled=False), 
        "original_word": st.column_config.TextColumn("원본 단어", disabled=True, width="large"),
        "root_word": "원형",
        "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
        "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사"])
    }
    cols = ["delete_check", "count", "original_word", "root_word", "origin", "pos"]
    
    edited_df = st.data_editor(df_display[cols] if not df_display.empty else df_display, column_config=column_config, use_container_width=True, num_rows="fixed", key="editor")
    
    btn_col1, btn_col2, btn_col3 = st.columns([1, 2, 2])
    
    with btn_col1:
        if st.button("⛔ 체크 삭제", type="secondary"):
            to_delete = edited_df[edited_df['delete_check'] == True]
            if not to_delete.empty and sheet:
                rows_to_add = []
                for _, row in to_delete.iterrows():
                     raw_orig = str(row['original_word']).split('(')[0] 
                     rows_to_add.append([datetime.now().isoformat(), raw_orig, row['root_word'], "", "", 'delete', input_text])
                
                if send_data_with_retry(sheet, rows_to_add, is_multiple=True):
                    st.toast(f"🗑️ 삭제 학습 완료!", icon="✅")
                    remaining = edited_df[edited_df['delete_check'] == False].to_dict('records')
                    st.session_state.analysis_result = remaining
                    time.sleep(1)
                    st.rerun()

    def save_logic(df_to_save, page_str):
        valid_rows = df_to_save[df_to_save['delete_check'] == False].copy()
        
        learning_logs = []
        for _, row in valid_rows.iterrows():
            c_origin = clean_value_for_save(row['origin'])
            c_pos = clean_value_for_save(row['pos'])
            learning_logs.append({
                'timestamp': datetime.now().isoformat(),
                'original_word': row['original_word'], 
                'root_word': row['root_word'],
                'origin': c_origin,
                'pos': c_pos,
                'action': 'modify',
                'context': input_text
            })
            
        def parse_count(val):
            try: return int(str(val).replace('회', '').strip())
            except: return 1
            
        valid_rows['numeric_count'] = valid_rows['count'].apply(parse_count)
        
        aggregated_df = valid_rows.groupby('root_word', as_index=False).agg({
            'numeric_count': 'sum', 
            'original_word': lambda x: ', '.join(x.unique()),
            'origin': 'first',
            'pos': 'first'
        })
        
        base_df = st.session_state.master_df
        if base_df is None: base_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])
        for c in base_df.columns:
            if '쪽수' in c: base_df[c] = base_df[c].astype(object)
            
        new_rows_for_excel = []
        
        for _, item in aggregated_df.iterrows():
            root = item['root_word']
            cnt = item['numeric_count']
            origin_val = clean_value_for_save(item['origin'])
            
            val = f"{page_str}_{cnt}" if cnt > 1 else page_str
            
            if root in base_df['자료'].values:
                idx = base_df[base_df['자료'] == root].index[0]
                filled = base_df.loc[idx].filter(like='쪽수').notna().sum()
                col = f"쪽수{filled+1}"
                if col not in base_df.columns: base_df[col] = float('nan')
                base_df.at[idx, col] = val
            else:
                new_rows_for_excel.append({'구분': origin_val, '자료': root, '쪽수1': val})
                
        if new_rows_for_excel:
            base_df = pd.concat([base_df, pd.DataFrame(new_rows_for_excel)], ignore_index=True)
            
        base_df['출연횟수'] = base_df.apply(calculate_total_appearances, axis=1)
        base_df['sort'] = base_df['구분'].map({'고':1, '순':1, '한':2, '외':3, '혼':4}).fillna(5)
        base_df = base_df.sort_values(['sort', '자료']).drop('sort', axis=1)
        st.session_state.master_df = base_df
        
        output_excel = io.BytesIO()
        with pd.ExcelWriter(output_excel, engine='openpyxl') as writer: base_df.to_excel(writer, index=False)
        output_excel.seek(0)
        st.session_state.excel_buffer = output_excel
        
        # [백업 기능 통합] 저장 시 자동 백업
        save_backup_to_cloud(MODE_KEY, base_df)

        if sheet and learning_logs:
            rows = [list(log.values()) for log in learning_logs]
            send_data_with_retry(sheet, rows, is_multiple=True)
            
        return True

    if is_pdf_mode:
        final_page_str = str(st.session_state.current_page_idx + st.session_state.start_page_offset)
    else:
        final_page_str = st.session_state.manual_page_input

    with btn_col2:
        if st.button("💾 저장하고 다음 쪽(▶) 이동", type="primary", use_container_width=True):
            if save_logic(edited_df, final_page_str):
                if is_pdf_mode and st.session_state.current_page_idx < st.session_state.total_pages - 1:
                    st.session_state.current_page_idx += 1
                    st.session_state.analysis_result = None
                    st.toast("✅ 저장 완료! 이동합니다.", icon="🏃")
                    time.sleep(1)
                    st.rerun()
                elif not is_pdf_mode:
                    st.success("이미지 파일은 다음 쪽이 없습니다. (저장 완료)")
                else:
                    st.success("마지막 페이지입니다!")

    with btn_col3:
        if st.button("💾 저장만 하기 (종료)", use_container_width=True):
            if save_logic(edited_df, final_page_str):
                st.success("✅ 저장되었습니다.")

    if st.session_state.excel_buffer:
        st.download_button(label="📥 엑셀파일 다운로드", data=st.session_state.excel_buffer, file_name="국어활동_분석결과_통합.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="secondary", use_container_width=True)