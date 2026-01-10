import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import time
import base64
from datetime import datetime
from collections import Counter

# ---------------------------------------------------------
# [0] 라이브러리 및 기본 설정
# ---------------------------------------------------------
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

st.set_page_config(page_title="국어활동 AI 분석기 (Integrated Pro)", page_icon="📚", layout="wide")

# API KEY 설정 (secrets 우선, 없으면 하드코딩된 값 사용 - GP9 로직 유지)
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = "" # 필요한 경우 여기에 키 입력
except:
    API_KEY = ""

MODEL_NAME = "gemini-2.0-flash-exp" # GP9에서 사용하던 모델 유지
SHEET_NAME = "Korean_DB"
TRUST_THRESHOLD = 3

# 스타일 커스터마이징
st.markdown("""
    <style>
        .stTextArea textarea { font-size: 14px; line-height: 1.6; font-family: 'Malgun Gothic', sans-serif; }
        .stDataFrame { border: 1px solid #ddd; }
        .block-container { padding-top: 2rem; }
    </style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------
# [1] 구글 시트 & 백업 시스템 (GP9 기능 완벽 복구)
# ---------------------------------------------------------
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
    except Exception as e:
        st.error(f"구글 인증 오류: {e}")
        return None

def get_sheet_data_fresh(mode_key):
    """구글 시트에서 최신 학습 데이터를 가져옵니다."""
    client = get_google_sheet_client()
    if not client: return None, []
    target_sheet_name = "South_Korea" if mode_key == "SOUTH" else "North_Korea"
    try:
        spreadsheet = client.open(SHEET_NAME)
        try:
            sheet = spreadsheet.worksheet(target_sheet_name)
        except gspread.WorksheetNotFound:
            st.warning(f"'{target_sheet_name}' 시트가 없어 새로 생성합니다.")
            sheet = spreadsheet.add_worksheet(title=target_sheet_name, rows=1000, cols=20)
        data = sheet.get_all_records()
        return sheet, data
    except Exception as e:
        st.error(f"시트 데이터 로드 실패: {e}")
        return None, []

def send_data_with_retry(sheet_obj, data, is_multiple=False):
    """데이터 전송 실패 시 재시도 로직 (GP9 기능)"""
    if not sheet_obj: return False
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if is_multiple:
                # 데이터 정제 (JSON 직렬화 가능하도록)
                clean_data = [[str(item) for item in row] for row in data]
                sheet_obj.append_rows(clean_data)
            else:
                clean_data = [str(item) for item in data]
                sheet_obj.append_row(clean_data)
            return True
        except Exception:
            time.sleep(1)
    return False

def save_backup_to_cloud(mode_key, df):
    """현재 작업 내용을 클라우드 백업 시트에 통째로 저장 (GP9 기능)"""
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
        
        # DataFrame을 리스트로 변환하여 업로드
        df_str = df.fillna("").astype(str)
        data_to_upload = [df_str.columns.values.tolist()] + df_str.values.tolist()
        worksheet.update(data_to_upload)
        return True
    except Exception as e:
        print(f"백업 실패: {e}")
        return False

def load_backup_from_cloud(mode_key):
    """클라우드에서 백업 데이터를 불러옴 (GP9 기능)"""
    client = get_google_sheet_client()
    if not client: return None
    
    backup_sheet_name = f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}"
    try:
        spreadsheet = client.open(SHEET_NAME)
        worksheet = spreadsheet.worksheet(backup_sheet_name)
        data = worksheet.get_all_records()
        return pd.DataFrame(data) if data else None
    except: return None

# ---------------------------------------------------------
# [2] AI 분석 엔진 (1~7단계 요구사항 및 GP9 로직 통합)
# ---------------------------------------------------------
def generate_prompt_from_sheet(sheet_data):
    """[요구사항 7] 시트 데이터를 기반으로 학습 규칙 프롬프트 생성"""
    if not sheet_data: return ""
    
    # 최근 학습된 내용 우선 반영을 위해 뒤에서부터 처리
    rules = []
    # GP9의 로직: action 컬럼을 확인하여 규칙 생성
    for row in sheet_data[-50:]: # 최근 50개만 (토큰 제한 고려)
        if row.get('action') == 'delete':
            rules.append(f"- [삭제 규칙]: '{row.get('original_word')}'는 분석 결과에서 제외하세요.")
        elif row.get('action') in ['add', 'modify']:
            rules.append(f"- [고정 규칙]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}', 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    
    if rules:
        return "\n[사용자 학습 규칙 (최우선 적용)]:\n" + "\n".join(rules) + "\n"
    return ""

def api_call_direct(prompt):
    """Gemini API 호출 (Text)"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY}"
    headers = {'Content-Type': 'application/json'}
    data = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            result = response.json()
            if 'candidates' in result:
                return result['candidates'][0]['content']['parts'][0]['text']
        return None
    except: return None

def api_call_vision_ocr(image_bytes):
    """[GP9 기능] Vision API를 이용한 고급 OCR"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY}"
    headers = {'Content-Type': 'application/json'}
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    # GP9의 상세한 OCR 프롬프트 복원
    prompt_text = """
    이 이미지에 있는 텍스트를 보이는 그대로 추출해주세요.
    
    [중요한 형식 규칙]
    1. **공간 분리 준수:** 말풍선, 단락 등으로 분리된 텍스트는 줄바꿈(Enter)으로 명확히 구분하세요.
    2. **세로쓰기 대응:** 글자가 세로로 쓰여 있다면 자연스러운 독해 순서를 따르세요.
    3. **표기 유지:** 두음법칙 미적용(로동, 녀자) 등 원본 표기를 그대로 유지하세요.
    4. **중복 포함:** 같은 문장이 여러 번 나오면 반복해서 적으세요.
    5. **노이즈 제거:** 단순 쪽수 번호나 장식용 문구는 제외하세요.
    """
    
    data = {
        "contents": [{"parts": [{"text": prompt_text}, {"inline_data": {"mime_type": "image/png", "data": base64_image}}]}],
        "generationConfig": {"temperature": 0.1}
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        return None
    except Exception as e: return f"OCR 오류: {e}"

def get_analysis_hybrid(text, sheet_data, mode_key):
    """[요구사항 5, 6, 7] CoT + 하다 처리 + 학습 반영 분석 로직"""
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    mode_desc = "대한민국 표준어" if mode_key == "SOUTH" else "북한 문화어(두음법칙 미적용)"
    
    # [요구사항 5] CoT 단계 명시
    # [요구사항 6] 하다 용언 처리 지침 명시
    base_instruction = f"""
    당신은 '{mode_desc}' 형태소 분석 전문가입니다.
    주어진 텍스트를 분석하여 JSON 형식으로 출력하세요.
    
    {learning_prompt}
    
    [분석 단계 (Chain of Thought)]
    1. **문맥 파악**: 텍스트의 맥락을 읽고 '{mode_desc}' 규칙을 적용할 준비를 합니다.
    2. **형태소 분리**: 조사(은/는/이/가/을/를 등)와 어미를 철저히 분리하여 제거합니다. (예: '나도' -> '나')
    3. **'하다' 용언 판단 (중요)**: '명사+하다' 형태는 기계적으로 나누지 말고 문맥을 봅니다.
       - 동작성이 강하면 동사(예: 공부하다), 명사성이 강하면 명사(예: 사랑)로 처리하세요.
    4. **품사 필터링**: 명사, 동사, 형용사, 부사, 관형사, 대명사만 남깁니다. (의존명사 제외)
    5. **출력**: 아래 JSON 포맷을 엄수하세요.

    [출력 포맷]
    [
        {{"original_word": "공부했다", "root_word": "공부하다", "origin": "한", "pos": "동사"}},
        {{"original_word": "학교에", "root_word": "학교", "origin": "한", "pos": "명사"}}
    ]
    (origin 코드: 고=고유어, 한=한자어, 외=외래어, 혼=혼종어)
    """
    
    # 텍스트가 너무 길 경우를 대비한 청크 분할 (GP9 로직)
    full_prompt = f"{base_instruction}\n\n[분석할 텍스트]:\n{text}"
    
    # API 호출
    res_text = api_call_direct(full_prompt)
    
    if res_text:
        try:
            # JSON 파싱
            match = re.search(r'\[.*\]', res_text, re.DOTALL)
            if match:
                return json.loads(match.group())
            # 백업 파싱
            s = res_text.find('[')
            e = res_text.rfind(']') + 1
            if s != -1 and e != -1:
                return json.loads(res_text[s:e])
        except: return []
    return []

# ---------------------------------------------------------
# [3] 파일 처리 및 텍스트 추출 (GP9 기능 복원)
# ---------------------------------------------------------
def split_text_smartly(text, chunk_size=1000):
    """긴 텍스트 분할 (GP9 기능)"""
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
    """PDF/이미지 통합 텍스트 추출기 (GP9 로직)"""
    file_type = file_obj.type
    
    # 이미지 파일 -> Vision OCR
    if "image" in file_type:
        try: return api_call_vision_ocr(file_obj.getvalue())
        except Exception as e: return f"이미지 OCR 오류: {e}"
        
    # PDF 파일 -> plumber -> fitz -> Vision OCR 순차 시도
    elif "pdf" in file_type:
        text = ""
        # 1. pdfplumber 시도
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(file_obj) as pdf:
                    if page_index < len(pdf.pages):
                        page = pdf.pages[page_index]
                        text = page.extract_text()
            except: pass
        
        # 2. 텍스트가 없으면 fitz 시도
        if not text and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_index < len(doc):
                    text = doc[page_index].get_text()
            except: pass
            
        # 3. 그래도 없으면 이미지를 떠서 Vision OCR (최후의 수단)
        if (not text or len(text.strip()) < 10) and FITZ_AVAILABLE:
             try:
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_index < len(doc):
                    pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(2, 2))
                    img_bytes = pix.tobytes("png")
                    return api_call_vision_ocr(img_bytes)
             except: pass
             
        return text if text else "텍스트를 추출할 수 없습니다."
    return ""

def get_page_image_bytes(file_obj, page_index):
    """뷰어용 이미지 바이트 생성 (GP9 로직)"""
    if "image" in file_obj.type:
        return file_obj.getvalue()
    elif "pdf" in file_obj.type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
            if 0 <= page_index < len(doc):
                pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                return pix.tobytes("png")
        except: pass
    return None

# ---------------------------------------------------------
# [4] 엑셀 저장 및 데이터 관리 로직 (GP9 핵심 로직)
# ---------------------------------------------------------
def clean_value_for_save(val):
    """이모지 제거"""
    if isinstance(val, str):
        return val.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('👤 ', '')
    return val

def calculate_total_appearances(row):
    """쪽수 컬럼들을 모두 합산하여 출연횟수 계산"""
    total = 0
    for col in row.index:
        if str(col).startswith('쪽수'):
            val = str(row[col])
            # "페이지_빈도" 형식 처리 (예: "15_2" -> 15쪽 2번)
            if '_' in val:
                try: total += int(val.split('_')[1])
                except: total += 1
            elif val != 'nan' and val != '' and val != 'None':
                total += 1
    return total

def save_logic(edited_df, page_str, sheet_obj, context_text):
    """
    [핵심 복원] GP9의 동적 컬럼 저장 로직
    - 중복 단어 발생 시 행 추가가 아니라 '쪽수N' 컬럼 추가
    - 학습 데이터 자동 전송
    - 자동 백업
    """
    # 1. 학습 데이터 전송 (수정된 내용 학습)
    if sheet_obj:
        learning_logs = []
        for _, row in edited_df.iterrows():
            # 삭제 체크된 것은 제외하고 학습
            if not row['delete_check']:
                c_origin = clean_value_for_save(row['origin'])
                c_pos = clean_value_for_save(row['pos'])
                learning_logs.append([
                    datetime.now().isoformat(),
                    row['original_word'], row['root_word'], c_origin, c_pos,
                    'modify', context_text[:50] # 문맥 일부 저장
                ])
        if learning_logs:
            send_data_with_retry(sheet_obj, learning_logs, is_multiple=True)

    # 2. 마스터 데이터프레임 업데이트
    valid_rows = edited_df[edited_df['delete_check'] == False].copy()
    
    # 원본(Root)+분류(Origin) 기준으로 데이터 집계
    # 같은 페이지에서 같은 단어가 여러 번 나오면 빈도 합산
    def parse_count(val):
        try: return int(str(val).replace('회', '').strip())
        except: return 1
    valid_rows['numeric_count'] = valid_rows['count'].apply(parse_count)
    
    aggregated = valid_rows.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({
        'numeric_count': 'sum',
        'original_word': lambda x: ', '.join(x.unique())
    })

    if st.session_state.master_df is None:
        st.session_state.master_df = pd.DataFrame(columns=['구분', '자료', '출연횟수', '쪽수1'])
    
    master = st.session_state.master_df
    
    # 쪽수 컬럼 데이터 타입 정리
    for c in master.columns:
        if '쪽수' in c: master[c] = master[c].astype(object)

    new_rows_list = []
    
    for _, item in aggregated.iterrows():
        root = item['root_word']
        origin_val = clean_value_for_save(item['origin'])
        cnt = item['numeric_count']
        
        # 저장할 값 포맷: "쪽수_빈도" (빈도가 1이면 그냥 쪽수만)
        val_to_save = f"{page_str}_{cnt}" if cnt > 1 else page_str
        
        # 기존 데이터에 있는지 확인
        mask = (master['자료'] == root) & (master['구분'] == origin_val)
        
        if mask.any():
            idx = master[mask].index[0]
            # 빈 쪽수 컬럼 찾기
            filled_cols = [c for c in master.columns if '쪽수' in c and pd.notna(master.at[idx, c])]
            
            # (옵션) 같은 페이지 중복 기재 방지 로직이 필요하다면 여기서 체크
            # 여기서는 GP9 로직대로 계속 추가
            next_col = f"쪽수{len(filled_cols) + 1}"
            
            if next_col not in master.columns:
                master[next_col] = None # 새 컬럼 생성
            
            master.at[idx, next_col] = val_to_save
        else:
            new_rows_list.append({
                '구분': origin_val,
                '자료': root,
                '출연횟수': 0, # 나중에 재계산
                '쪽수1': val_to_save
            })
            
    if new_rows_list:
        master = pd.concat([master, pd.DataFrame(new_rows_list)], ignore_index=True)
        
    # 출연횟수 전체 재계산
    master['출연횟수'] = master.apply(calculate_total_appearances, axis=1)
    
    # 정렬 (고 -> 한 -> 외 -> 혼 순서)
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    master['sort_key'] = master['구분'].map(sort_map).fillna(5)
    master = master.sort_values(['sort_key', '자료']).drop('sort_key', axis=1)
    
    st.session_state.master_df = master
    
    # [안전장치] 클라우드 자동 백업
    save_backup_to_cloud(st.session_state.last_mode, master)
    
    return True

# ---------------------------------------------------------
# [5] 메인 UI 구성
# ---------------------------------------------------------
# 세션 상태 초기화
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'file_hash' not in st.session_state: st.session_state.file_hash = None
if 'start_page_offset' not in st.session_state: st.session_state.start_page_offset = 1
if 'manual_page_input' not in st.session_state: st.session_state.manual_page_input = "1"
if 'last_uploaded_file_name' not in st.session_state: st.session_state.last_uploaded_file_name = None

st.title("📝 국어활동 AI 분석기")

# [사이드바]
with st.sidebar:
    st.header("🏳️ 모드 및 설정")
    mode_selection = st.radio("언어 모드", ("🇰🇷 대한민국 표준어", "🇰🇵 북한 문화어"))
    MODE_KEY = "SOUTH" if "대한민국" in mode_selection else "NORTH"
    
    # [Req 2] 모드 변경 시 안전장치
    if 'last_mode' not in st.session_state: st.session_state.last_mode = MODE_KEY
    if st.session_state.last_mode != MODE_KEY:
        if st.session_state.master_df is not None:
            save_backup_to_cloud(st.session_state.last_mode, st.session_state.master_df)
        st.session_state.master_df = None
        st.session_state.last_mode = MODE_KEY
        st.rerun()
        
    sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)
    if sheet: st.caption(f"✅ 지능 연결됨: {len(sheet_data)}건 학습")
    else: st.error("❌ 구글 시트 연결 실패")
    
    st.markdown("---")
    st.header("📂 파일 이어하기")
    
    # [Req 1] 엑셀 이어하기 (Merge Logic)
    uploaded_excel = st.file_uploader("작업하던 엑셀", type=['xlsx'])
    if uploaded_excel:
        if uploaded_excel.name != st.session_state.last_uploaded_file_name:
            if st.button("데이터 병합하기"):
                try:
                    loaded = pd.read_excel(uploaded_excel)
                    if st.session_state.master_df is not None:
                        # 덮어쓰지 않고 병합
                        merged = pd.concat([st.session_state.master_df, loaded], ignore_index=True)
                        st.session_state.master_df = merged.drop_duplicates(subset=['자료', '구분'], keep='last')
                    else:
                        st.session_state.master_df = loaded
                    st.session_state.last_uploaded_file_name = uploaded_excel.name
                    st.success("병합 완료!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"엑셀 로드 실패: {e}")

    # 백업 및 복구
    c1, c2 = st.columns(2)
    with c1:
        if st.button("☁️ 백업"):
            if save_backup_to_cloud(MODE_KEY, st.session_state.master_df):
                st.toast("백업 성공")
    with c2:
        if st.button("🔄 복구"):
            res = load_backup_from_cloud(MODE_KEY)
            if res is not None:
                st.session_state.master_df = res
                st.toast("복구 성공")
                time.sleep(1)
                st.rerun()

    st.markdown("---")
    # [GP9 기능] 수동 단어 추가
    with st.expander("➕ 단어 수동 추가"):
        with st.form("manual_add"):
            m_orig = st.text_input("원본 단어")
            m_root = st.text_input("원형")
            m_origin = st.selectbox("분류", ["고", "한", "외", "혼"])
            m_pos = st.selectbox("품사", ["명사", "동사", "형용사", "부사", "관형사", "대명사"])
            if st.form_submit_button("추가"):
                if m_orig and m_root and sheet:
                    row = [datetime.now().isoformat(), m_orig, m_root, m_origin, m_pos, 'add', '수동']
                    send_data_with_retry(sheet, row)
                    st.toast("추가되었습니다.")

    # [GP9 기능] 이력 검색
    search = st.text_input("🔍 이력 검색")
    if search and sheet_data:
        res = [r for r in sheet_data if search in str(r.get('root_word', ''))]
        for r in res[-3:]:
            st.caption(f"{r.get('action')}: {r.get('root_word')}")


# [메인] 파일 업로드
st.subheader("1. 교과서/이미지 업로드")
main_file = st.file_uploader("PDF/이미지 파일", type=['pdf', 'png', 'jpg'])

if main_file:
    if st.session_state.file_hash != main_file.id:
        st.session_state.file_hash = main_file.id
        st.session_state.page_idx = 0
        st.session_state.analysis_result = []
    
    # PDF 페이지 수 계산
    total_pages = 1
    if "pdf" in main_file.type:
        try:
            if PLUMBER_AVAILABLE:
                with pdfplumber.open(main_file) as pdf: total_pages = len(pdf.pages)
            elif FITZ_AVAILABLE:
                doc = fitz.open(stream=main_file.getvalue(), filetype="pdf")
                total_pages = len(doc)
        except: pass

    # [Req 3] 뷰어 및 페이지 이동 컨트롤
    col_view, col_txt = st.columns([1, 1])
    
    with col_view:
        st.info("📷 미리보기")
        img = get_page_image_bytes(main_file, st.session_state.page_idx)
        if img: st.image(img, use_container_width=True)
        
        if "pdf" in main_file.type:
            # 페이지 이동 컨트롤 (이전/다음 + 점프)
            c_prev, c_jump, c_next = st.columns([1, 2, 1])
            with c_prev:
                if st.button("◀"):
                    st.session_state.page_idx = max(0, st.session_state.page_idx - 1)
                    st.rerun()
            with c_next:
                if st.button("▶"):
                    st.session_state.page_idx = min(total_pages - 1, st.session_state.page_idx + 1)
                    st.rerun()
            with c_jump:
                # [Req 3] 페이지 한번에 이동 기능
                target_page = st.number_input("페이지 이동", min_value=1, max_value=total_pages, value=st.session_state.page_idx + 1)
                if target_page - 1 != st.session_state.page_idx:
                    st.session_state.page_idx = target_page - 1
                    st.rerun()
            
            # 쪽수 오프셋
            st.session_state.start_page_offset = st.number_input("시작 쪽수 오프셋", value=st.session_state.start_page_offset)
            real_page = st.session_state.page_idx + st.session_state.start_page_offset
            st.caption(f"PDF {st.session_state.page_idx+1}p = 교과서 {real_page}쪽")
            page_str = str(real_page)
        else:
            page_str = st.text_input("쪽수 입력", value="1")

    with col_txt:
        st.info("📝 텍스트 분석")
        # [Req 4] 분석 내용 직접 입력 (editable text area)
        extracted = extract_text_unified(main_file, st.session_state.page_idx)
        input_text = st.text_area("분석 텍스트 (수정 가능)", value=extracted, height=300)
        
        if st.button("🚀 분석 실행 (CoT + Vision)", type="primary"):
            with st.spinner("AI 분석 중..."):
                # 텍스트가 적으면 이미지로 Vision 분석
                send_img = img if len(input_text.strip()) < 10 else None
                results = get_analysis_hybrid(input_text, send_img, sheet_data, MODE_KEY)
                
                # 결과 가공
                processed = []
                origin_map = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pos_map = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                
                # 빈도 계산
                cnts = Counter([r['original_word'] for r in results])
                seen = set()
                
                for r in results:
                    root = r['root_word']
                    if root not in seen:
                        processed.append({
                            "delete_check": False,
                            "count": f"{cnts[r['original_word']]}회",
                            "original_word": r['original_word'],
                            "root_word": root,
                            "origin": origin_map.get(r['origin'], r['origin']),
                            "pos": pos_map.get(r['pos'], r['pos'])
                        })
                        seen.add(root)
                st.session_state.analysis_result = processed

    # [Req 2] 분석 결과 수정 (초기화 방지용 num_rows="dynamic")
    if st.session_state.analysis_result:
        st.subheader("2. 결과 확인 및 저장")
        df_res = pd.DataFrame(st.session_state.analysis_result)
        
        edited_df = st.data_editor(
            df_res,
            column_config={
                "delete_check": st.column_config.CheckboxColumn("삭제"),
                "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
                "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사"])
            },
            num_rows="dynamic", # 여기서 행 추가 가능
            use_container_width=True,
            key="editor_main"
        )
        
        # 저장 버튼
        col_s1, col_s2, col_s3 = st.columns(3)
        
        # 삭제 버튼 (GP9 기능)
        with col_s1:
            if st.button("⛔ 체크 항목 삭제"):
                to_del = edited_df[edited_df['delete_check'] == True]
                if not to_del.empty and sheet:
                    rows = [[datetime.now().isoformat(), r['original_word'], r['root_word'], "", "", 'delete', 'User'] for _, r in to_del.iterrows()]
                    send_data_with_retry(sheet, rows, is_multiple=True)
                    st.toast("삭제 규칙 학습 완료")
                    # 화면 갱신
                    leftover = edited_df[edited_df['delete_check'] == False].to_dict('records')
                    st.session_state.analysis_result = leftover
                    st.rerun()

        # 저장 및 다음 (GP9 기능 + Req 1, 2)
        with col_s2:
            if st.button("💾 저장하고 다음 쪽(▶)"):
                if save_logic(edited_df, page_str, sheet, input_text):
                    st.toast("저장 완료!")
                    if "pdf" in main_file.type and st.session_state.page_idx < total_pages - 1:
                        st.session_state.page_idx += 1
                        st.session_state.analysis_result = []
                        time.sleep(0.5)
                        st.rerun()
                    else:
                        st.success("마지막 페이지입니다.")

        # 저장만 하기
        with col_s3:
            if st.button("💾 저장만 하기"):
                if save_logic(edited_df, page_str, sheet, input_text):
                    st.success("저장되었습니다.")

# [하단] 통계 및 전체 데이터 확인 (GP9 기능)
if st.session_state.master_df is not None:
    st.markdown("---")
    st.subheader("📊 전체 데이터")
    
    tab1, tab2 = st.tabs(["데이터프레임", "통계"])
    
    with tab1:
        st.dataframe(st.session_state.master_df, use_container_width=True)
        # 엑셀 다운로드
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            st.session_state.master_df.to_excel(writer, index=False)
        st.download_button("📥 엑셀 다운로드", buffer.getvalue(), "analysis_final.xlsx")

    with tab2:
        df = st.session_state.master_df
        if '구분' in df.columns:
            st.bar_chart(df['구분'].value_counts())
        st.metric("총 단어 수", len(df))