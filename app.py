import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import os
import time
import base64
from datetime import datetime
from collections import Counter

# =========================================================
# [0] 라이브러리 임포트 및 상태 체크 (GP9 원본 안전장치 유지)
# =========================================================
# 각 라이브러리 설치 여부를 확인하고 플래그를 설정합니다.
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

# 페이지 기본 설정 (와이드 모드, 아이콘 설정)
st.set_page_config(
    page_title="국어활동 AI 분석기 (Ultimate Fixed)", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# =========================================================
# [1] API 및 기본 설정
# =========================================================
# API 키 로드 (secrets.toml 우선, 없으면 로컬 변수 사용)
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = "" # 로컬 테스트용
except:
    API_KEY = ""

MODEL_NAME = "gemini-2.0-flash-exp"
SHEET_NAME = "Korean_DB"
TRUST_THRESHOLD = 3 

# [CSS 수정] 사용자 요청: 텍스트창 다크 모드 (진한 회색 배경 + 흰색 글씨)
st.markdown("""
    <style>
        /* 텍스트 입력창 스타일링 (눈 보호 모드) */
        .stTextArea textarea { 
            font-size: 16px !important; 
            line-height: 1.6 !important; 
            font-family: 'Malgun Gothic', sans-serif !important; 
            background-color: #262730 !important; /* 진한 회색 배경 */
            color: #ffffff !important; /* 흰색 글씨 */
            border: 1px solid #4a4a4a !important; /* 어두운 테두리 */
            font-weight: 400 !important;
        }
        /* 입력창 포커스 시 테두리 강조 */
        .stTextArea textarea:focus {
            border: 1px solid #ff4b4b !important;
        }
        
        /* 데이터프레임 테두리 */
        .stDataFrame { border: 1px solid #ddd; }
        
        /* 상단 여백 조정 */
        .block-container { padding-top: 2rem; }
        
        /* 확장 패널 폰트 조정 */
        div[data-testid="stExpander"] details summary p {
            font-size: 1.05rem;
            font-weight: 600;
        }
        
        /* 버튼 스타일 강화 */
        div.stButton > button {
            font-weight: bold;
        }
    </style>
""", unsafe_allow_html=True)

# =========================================================
# [2] 구글 시트 & 백업 시스템 (GP9 원본 로직 100% 복구)
# =========================================================
@st.cache_resource
def get_google_sheet_client():
    """구글 시트 인증 클라이언트 생성 (예외 처리 포함)"""
    if not GSPREAD_AVAILABLE: return None
    try:
        if "gcp_service_account" not in st.secrets:
            return None
        
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        
        # TOML 파일의 개행 문자 처리
        if "private_key" in creds_dict:
             creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        st.error(f"⚠️ 구글 인증 에러: {e}")
        return None

def get_sheet_data_fresh(mode_key):
    """모드(남/북)에 맞는 시트 데이터 로드 및 시트 자동 생성"""
    client = get_google_sheet_client()
    if not client: return None, []
    
    target_sheet_name = "South_Korea" if mode_key == "SOUTH" else "North_Korea"
    
    try:
        spreadsheet = client.open(SHEET_NAME)
        try:
            sheet = spreadsheet.worksheet(target_sheet_name)
        except gspread.WorksheetNotFound:
            # 시트가 없으면 자동으로 생성
            st.warning(f"⚠️ '{target_sheet_name}' 시트가 없어 새로 생성합니다.")
            sheet = spreadsheet.add_worksheet(title=target_sheet_name, rows=1000, cols=20)
            # 헤더 추가
            sheet.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context"])
            
        data = sheet.get_all_records()
        return sheet, data
    except Exception as e:
        # 연결 실패 시 조용히 넘어가거나 로그 출력
        return None, []

def send_data_with_retry(sheet_obj, data, is_multiple=False):
    """데이터 전송 실패 시 재시도 로직 (GP9의 안전장치)"""
    if not sheet_obj: return False
    
    max_retries = 3
    for i in range(max_retries):
        try:
            if is_multiple:
                # 대량 전송: 모든 데이터를 문자열로 변환하여 전송
                clean_data = [[str(item) for item in row] for row in data]
                sheet_obj.append_rows(clean_data)
            else:
                # 단건 전송
                clean_data = [str(item) for item in data]
                sheet_obj.append_row(clean_data)
            return True
        except Exception as e:
            if i < max_retries - 1:
                time.sleep(1) # 잠시 대기 후 재시도
                continue
            else:
                st.error(f"❌ 데이터 전송 최종 실패: {str(e)}")
                return False
    return False

def save_backup_to_cloud(mode_key, df):
    """[핵심] 현재 작업 데이터를 클라우드 백업 시트에 통째로 저장"""
    client = get_google_sheet_client()
    if not client or df is None or df.empty: return False
    
    backup_sheet_name = f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}"
    
    try:
        spreadsheet = client.open(SHEET_NAME)
        try:
            worksheet = spreadsheet.worksheet(backup_sheet_name)
            worksheet.clear() # 기존 데이터 삭제
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=backup_sheet_name, rows=1000, cols=20)
        
        # DataFrame을 리스트로 변환 (NaN 처리 포함)
        df_str = df.fillna("").astype(str)
        data_to_upload = [df_str.columns.values.tolist()] + df_str.values.tolist()
        
        worksheet.update(data_to_upload)
        return True
    except Exception as e:
        print(f"자동 백업 실패: {e}") 
        return False

def load_backup_from_cloud(mode_key):
    """클라우드에서 백업 데이터를 불러옴"""
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
# [3] AI 엔진 (1~7단계 요구사항 반영, '의미' 열 삭제)
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    """[Req 7] 구글 시트 데이터를 기반으로 학습 규칙 프롬프트 생성"""
    if not sheet_data: return ""
    
    rules = []
    # GP9의 로직대로 action 컬럼 분석
    # 최근 학습된 내용 우선 반영을 위해 뒤에서부터 처리 (50개 제한)
    for row in sheet_data[-50:]:
        if row.get('action') == 'delete':
            rules.append(f"- [삭제 규칙]: '{row.get('original_word')}'는 분석 결과에서 제외하세요.")
        elif row.get('action') in ['add', 'modify']:
            # 의미(meaning) 제거됨
            rules.append(f"- [고정 규칙]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}', 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    
    if rules:
        return "\n[🚨 최우선 사용자 학습 규칙 (이것이 법이다)]:\n" + "\n".join(rules) + "\n"
    return ""

def api_call_direct(prompt, image_bytes=None):
    """Gemini API 호출 (Text Only 또는 Vision)"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    
    parts = [{"text": prompt}]
    
    if image_bytes:
        # Vision 모드: 이미지를 base64로 인코딩하여 전송
        b64_image = base64.b64encode(image_bytes).decode('utf-8')
        parts.append({"inline_data": {"mime_type": "image/png", "data": b64_image}})
    
    data = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1, # 정확도 우선
            "maxOutputTokens": 8192
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300) # 타임아웃 300초 (GP9 설정)
        if response.status_code != 200:
            return None
            
        result_json = response.json()
        if 'candidates' in result_json:
            return result_json['candidates'][0]['content']['parts'][0]['text']
        return None
    except Exception as e:
        return None

def api_call_vision_ocr(image_bytes):
    """[GP9 기능] Vision API를 이용한 정밀 OCR"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    # GP9의 상세한 OCR 프롬프트 복원
    prompt_text = """
    이 이미지에 있는 텍스트를 보이는 그대로 추출해주세요.
    
    [중요한 형식 규칙]
    1. **공간 분리 준수:** 말풍선, 단락, 표 등으로 시각적으로 분리된 텍스트 덩어리는 반드시 **줄바꿈(Enter)**으로 명확히 구분하세요.
    2. **세로쓰기 대응:** 글자가 세로로(위에서 아래로) 쓰여 있다면, 자연스러운 독해 순서(우측 상단 -> 좌측 하단)를 따르세요.
    3. **표기 유지:** 두음법칙을 적용하지 않은 표기(예: 로동, 녀자)는 수정하지 말고 그대로 적으세요.
    4. **중복 포함(필수):** 같은 단어나 문장이 여러 번 나오면 합치지 말고 **나온 횟수만큼 반복해서** 적으세요.
    5. **노이즈 제거:** 쪽수, 머리말은 제외하세요.
    """
    
    data = {
        "contents": [{"parts": [{"text": prompt_text}, {"inline_data": {"mime_type": "image/png", "data": base64_image}}]}],
        "generationConfig": {"temperature": 0.1}
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=300)
        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        return ""
    except Exception as e:
        return f"OCR 통신 오류: {e}"

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    """
    [요구사항 5, 6, 7] CoT + 하다 처리 + 학습 반영 통합 분석
    """
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    mode_desc = "대한민국 표준어" if mode_key == "SOUTH" else "북한 문화어(두음법칙 미적용)"
    
    # [CoT 프롬프트 설계 - 의미(Meaning) 제거됨]
    prompt = f"""
    당신은 '{mode_desc}' 형태소 분석 및 국어사전 편찬 전문가입니다.
    주어진 텍스트(또는 이미지 속 텍스트)를 분석하여 JSON 형식으로 출력하세요.
    
    {learning_prompt}
    
    [분석 단계 (Chain of Thought)]
    1. **문맥 파악**: 텍스트의 맥락을 읽고 '{mode_desc}' 규칙을 적용하세요.
    2. **형태소 분리**: 조사(은/는/이/가/을/를/에/에게 등)와 어미를 철저히 분리하여 제거합니다.
       - 예: '친구를' -> '친구', '학교에서' -> '학교'
    3. **'하다' 용언 처리 (중요)**: '명사+하다' 형태는 기계적으로 나누지 말고 문맥을 봅니다.
       - 동작성이 강하면 동사 (예: '공부하다', '생각하다')
       - 명사성이 강하면 명사 (예: '사랑', '감사')
       - 문맥상 더 자연스러운 원형을 선택하세요.
    4. **품사 필터링**: 명사, 동사, 형용사, 부사, 관형사, 대명사만 남깁니다. (의존명사, 수사 제외)
    5. **출력**: 아래 JSON 포맷을 엄수하세요. (코드 블록 없이 순수 JSON만 출력하면 더 좋습니다)

    [JSON 예시]
    [
        {{"original_word": "배를", "root_word": "배", "origin": "고", "pos": "명사"}},
        {{"original_word": "공부했다", "root_word": "공부하다", "origin": "한", "pos": "동사"}}
    ]
    (origin 코드: 고=고유어, 한=한자어, 외=외래어, 혼=혼종어)
    """
    
    user_msg = f"[분석할 텍스트]:\n{text}"
    if image_bytes: user_msg = "위 이미지의 텍스트를 읽고(OCR), 위 분석 규칙대로 분석하여 JSON으로 출력하세요."
    
    full_prompt = f"{prompt}\n\n{user_msg}"
    
    # API 호출
    res_text = api_call_direct(full_prompt, image_bytes)
    
    if res_text:
        try:
            # JSON 파싱 (코드블록 제거 처리)
            match = re.search(r'\[.*\]', res_text, re.DOTALL)
            if match:
                return json.loads(match.group())
            
            # 백업 파싱 (대괄호 찾기)
            s = res_text.find('[')
            e = res_text.rfind(']') + 1
            if s != -1 and e != -1:
                return json.loads(res_text[s:e])
        except Exception as e:
            # 파싱 실패 시 빈 리스트 반환
            print(f"JSON Parsing Error: {e}")
            return []
    return []

# =========================================================
# [4] 파일 처리 및 텍스트 추출 (_GP9 복원 및 강화)
# =========================================================
def extract_text_unified(file_obj, page_idx):
    """PDF/이미지 통합 텍스트 추출기 (폴백 로직 포함)"""
    file_type = file_obj.type
    
    # 이미지는 Vision으로 처리하므로 텍스트 추출 안 함 (빈 문자열 반환)
    if "image" in file_type:
        try: return api_call_vision_ocr(file_obj.getvalue())
        except: return ""
        
    elif "pdf" in file_type:
        text = ""
        # 1. pdfplumber 시도 (텍스트 레이어 추출)
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(file_obj) as pdf:
                    if page_idx < len(pdf.pages):
                        page = pdf.pages[page_idx]
                        text = page.extract_text()
            except: pass
        
        # 2. 텍스트가 없으면 fitz 시도 (PyMuPDF)
        if not text and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_idx < len(doc):
                    text = doc[page_idx].get_text()
            except: pass
        
        # 3. 텍스트가 여전히 없거나 너무 짧으면(이미지형 PDF), Vision OCR 시도 (GP9의 강력한 기능)
        if (not text or len(text.strip()) < 10) and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_idx < len(doc):
                    pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2, 2))
                    img_bytes = pix.tobytes("png")
                    return api_call_vision_ocr(img_bytes)
            except: pass
            
        return text if text else ""
        
    return ""

def get_page_image_bytes(file_obj, page_idx):
    """뷰어용 이미지 바이트 생성 (미리보기)"""
    file_type = file_obj.type
    
    if "image" in file_type:
        return file_obj.getvalue()
        
    elif "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
            if 0 <= page_idx < len(doc):
                # 해상도 높임 (Matrix 1.5 -> 2.0 권장)
                pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0)) 
                return pix.tobytes("png")
        except: pass
    return None

# =========================================================
# [5] 데이터 저장 로직 (_GP9 핵심 - 동적 컬럼)
# =========================================================
def clean_val(v):
    """이모지 및 특수문자 제거"""
    if isinstance(v, str):
        return v.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('👤 ', '')
    return v

def calc_freq(row):
    """쪽수 컬럼들을 모두 합산하여 출연횟수 자동 계산"""
    total = 0
    for c in row.index:
        if str(c).startswith('쪽수'):
            v = str(row[c])
            # "15_2" 형식 처리 (15쪽 2번)
            if '_' in v: 
                try: total += int(v.split('_')[1])
                except: total += 1
            elif v not in ['nan', '', 'None']: 
                total += 1
    return total

def save_logic(edited_df, page_str, sheet_obj, context_text):
    """
    [핵심 복원] GP9의 동적 컬럼 저장 및 자동 백업 로직
    - 중복 단어 발생 시 행 추가가 아니라 '쪽수N' 컬럼 추가
    - 학습 데이터 자동 전송
    - 클라우드 자동 백업
    """
    # 1. 학습 데이터 전송 ('의미' 제외)
    if sheet_obj:
        logs = []
        for _, row in edited_df.iterrows():
            # 삭제 체크되지 않은 유효한 데이터만 학습
            if not row['delete_check']:
                logs.append([
                    datetime.now().isoformat(),
                    row['original_word'], 
                    row['root_word'], 
                    clean_val(row['origin']), 
                    clean_val(row['pos']), 
                    'modify', 
                    context_text[:50]
                ])
        if logs: 
            send_data_with_retry(sheet_obj, logs, is_multiple=True)

    # 2. 마스터 데이터프레임 업데이트 (엑셀 저장용)
    # 삭제되지 않은 행만 필터링
    valid_rows = edited_df[edited_df['delete_check'] == False].copy()
    
    # 빈도 문자열 파싱 (예: "3회" -> 3)
    def parse_count(val):
        try: return int(str(val).replace('회', '').strip())
        except: return 1
    valid_rows['numeric_count'] = valid_rows['count'].apply(parse_count)
    
    # '의미' 열 삭제되었으므로 제외하고 그룹화
    aggregated = valid_rows.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({
        'numeric_count': 'sum', 
        'original_word': lambda x: ', '.join(x.unique())
    })
    
    # 마스터 DF 초기화
    if st.session_state.master_df is None:
        st.session_state.master_df = pd.DataFrame(columns=['구분', '자료', '출연횟수', '쪽수1'])
    
    master = st.session_state.master_df
    
    # 쪽수 컬럼 데이터 타입 정리 (문자열 등 허용)
    for c in master.columns:
        if '쪽수' in c: master[c] = master[c].astype(object)

    new_rows_list = []
    
    for _, item in aggregated.iterrows():
        root = item['root_word']
        origin_val = clean_val(item['origin'])
        cnt = item['numeric_count']
        
        # 저장할 값 포맷: "쪽수_빈도" (빈도가 1보다 크면)
        val_to_save = f"{page_str}_{cnt}" if cnt > 1 else page_str
        
        # 기존 데이터에 있는지 확인 (자료 + 구분) - 의미 제외됨
        mask = (master['자료'] == root) & (master['구분'] == origin_val)
        
        if mask.any():
            # 이미 있으면 해당 행의 인덱스를 찾음
            idx = master[mask].index[0]
            
            # 빈 쪽수 컬럼 찾기
            filled_cols = [c for c in master.columns if '쪽수' in c and pd.notna(master.at[idx, c])]
            
            # 다음 컬럼 이름 생성 (예: 쪽수5)
            next_col = f"쪽수{len(filled_cols) + 1}"
            
            # 컬럼이 없으면 새로 생성
            if next_col not in master.columns:
                master[next_col] = None 
            
            # 값 기입
            master.at[idx, next_col] = val_to_save
        else:
            # 없으면 새 행 추가
            new_rows_list.append({
                '구분': origin_val,
                '자료': root,
                '출연횟수': 0, # 나중에 재계산
                '쪽수1': val_to_save
            })
            
    # 새 행들을 마스터에 병합
    if new_rows_list:
        master = pd.concat([master, pd.DataFrame(new_rows_list)], ignore_index=True)
        
    # 출연횟수 전체 재계산 (쪽수 컬럼 기반)
    master['출연횟수'] = master.apply(calc_freq, axis=1)
    
    # 정렬 (고 -> 한 -> 외 -> 혼 순서)
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    master['sort_key'] = master['구분'].map(sort_map).fillna(5)
    master = master.sort_values(['sort_key', '자료']).drop('sort_key', axis=1)
    
    # 세션 업데이트
    st.session_state.master_df = master
    
    # [GP9 기능] 자동 백업 실행
    if save_backup_to_cloud(st.session_state.last_mode, master):
        pass 
    else:
        st.toast("⚠️ 로컬 저장은 되었으나, 클라우드 백업에 실패했습니다.", icon="☁️")
        
    return True

# =========================================================
# [6] 메인 UI 구성
# =========================================================
# 세션 상태 변수 초기화 (새로고침 시 데이터 증발 방지)
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'file_hash' not in st.session_state: st.session_state.file_hash = None
if 'start_page_offset' not in st.session_state: st.session_state.start_page_offset = 1
if 'manual_page_input' not in st.session_state: st.session_state.manual_page_input = "1"
if 'last_uploaded_file_name' not in st.session_state: st.session_state.last_uploaded_file_name = None
# [핵심] 입력 텍스트 상태 보존용 (새로고침 방지)
if 'editor_text_content' not in st.session_state: st.session_state.editor_text_content = ""

st.title("📝 국어활동 AI 분석기")

# ----------------------------
# [사이드바] 설정 및 도구
# ----------------------------
with st.sidebar:
    st.header("⚙️ 설정")
    mode = st.radio("언어 모드", ["🇰🇷 표준어", "🇰🇵 문화어"])
    MODE_KEY = "SOUTH" if "표준어" in mode else "NORTH"
    
    # 모드 변경 감지 및 자동 백업/리셋
    if 'last_mode' not in st.session_state: st.session_state.last_mode = MODE_KEY
    if st.session_state.last_mode != MODE_KEY:
        # 데이터가 있으면 백업 후 초기화
        if st.session_state.master_df is not None:
            save_backup_to_cloud(st.session_state.last_mode, st.session_state.master_df)
        st.session_state.master_df = None
        st.session_state.last_mode = MODE_KEY
        st.rerun() # 화면 갱신
        
    sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)
    if sheet: 
        st.caption(f"✅ 학습 데이터 연결됨: {len(sheet_data)}건")
    else: 
        st.error("❌ 구글 시트 연결 실패")
    
    st.markdown("---")
    st.header("📂 이어하기")
    
    # [Req 1] 엑셀 이어하기 (Merge Logic - 덮어쓰지 않음)
    up_excel = st.file_uploader("엑셀 파일 선택", type=['xlsx'])
    
    if up_excel and up_excel.name != st.session_state.last_uploaded_file_name:
        if st.button("병합하기"):
            try:
                loaded = pd.read_excel(up_excel)
                if st.session_state.master_df is not None:
                    # 기존 데이터 + 새 데이터 병합 (중복 제거)
                    # 의미 열 없이 자료, 구분 기준으로 병합
                    cols = ['자료', '구분']
                    m = pd.concat([st.session_state.master_df, loaded]).drop_duplicates(subset=cols, keep='first')
                    st.session_state.master_df = m
                else:
                    st.session_state.master_df = loaded
                
                st.session_state.last_uploaded_file_name = up_excel.name
                st.success("데이터 병합 완료! (기존 데이터 보존됨)")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.error(f"엑셀 로드 오류: {e}")

    # 백업 및 복구 버튼
    c1, c2 = st.columns(2)
    with c1:
        if st.button("☁️ 강제 백업"): 
            if save_backup_to_cloud(MODE_KEY, st.session_state.master_df): 
                st.toast("클라우드 백업 성공", icon="☁️")
    with c2:
        if st.button("🔄 복구"):
            r = load_backup_from_cloud(MODE_KEY)
            if r is not None: 
                st.session_state.master_df = r
                st.toast("복구 성공", icon="✅")
                time.sleep(1)
                st.rerun()
            else:
                st.warning("백업 데이터가 없습니다.")

    st.markdown("---")
    # [GP9 기능] 수동 추가 폼
    with st.expander("➕ 단어 수동 추가"):
        with st.form("manual_add"):
            st.caption("AI가 놓친 단어를 직접 학습시킵니다.")
            o = st.text_input("원본 단어 (예: 책을)")
            r = st.text_input("원형 (예: 책)")
            org = st.selectbox("분류", ["고","한","외","혼"])
            p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사"])
            
            if st.form_submit_button("추가 및 학습"):
                if o and r and sheet:
                    # 의미 제외하고 전송
                    send_data_with_retry(sheet, [
                        datetime.now().isoformat(), o, r, org, p, 'add', '수동추가'
                    ])
                    st.toast(f"'{r}' 단어가 추가되었습니다.")

    st.markdown("---")
    st.caption("🔍 학습 이력 검색")
    search_q = st.text_input("단어 검색", placeholder="예: 사랑")
    if search_q and sheet_data:
        # 최근 이력부터 검색
        found = [row for row in sheet_data if search_q in str(row.get('root_word')) or search_q in str(row.get('original_word'))]
        if found:
            for f in found[-3:]: # 최근 3개만 표시
                st.text(f"[{f.get('action')}] {f.get('root_word')} ({f.get('origin')})")
        else:
            if search_q: st.caption("검색 결과가 없습니다.")

# ----------------------------
# [메인 화면]
# ----------------------------
st.subheader("1. 분석 자료 입력")

# 파일 업로드 (PDF/이미지)
main_file = st.file_uploader("PDF/이미지 파일 (선택)", type=['pdf', 'png', 'jpg'])

# 파일 변경 감지 로직
if main_file:
    if st.session_state.file_hash != main_file.id:
        st.session_state.file_hash = main_file.id
        st.session_state.page_idx = 0
        st.session_state.analysis_result = []
        # 새 파일이면 첫 페이지 텍스트 추출하여 입력창에 세팅
        st.session_state.editor_text_content = extract_text_unified(main_file, 0)
        st.rerun()
else:
    # 파일이 없으면 기존 텍스트 유지 (직접 입력 모드)
    pass

# PDF 페이지 수 계산
total_pages = 1
if main_file and "pdf" in main_file.type:
    try:
        if PLUMBER_AVAILABLE:
            with pdfplumber.open(main_file) as pdf: total_pages = len(pdf.pages)
        elif FITZ_AVAILABLE:
            doc = fitz.open(stream=main_file.getvalue(), filetype="pdf")
            total_pages = len(doc)
    except: pass

col_view, col_input = st.columns([1, 1])

# [좌측] 뷰어 및 네비게이션
with col_view:
    if main_file:
        st.info("📷 미리보기")
        img_bytes = get_page_image_bytes(main_file, st.session_state.page_idx)
        if img_bytes: 
            st.image(img_bytes, use_container_width=True)
        else:
            st.warning("미리보기를 생성할 수 없습니다.")
        
        if "pdf" in main_file.type:
            # 페이지 이동 컨트롤
            c_prev, c_jump, c_next = st.columns([1, 2, 1])
            with c_prev:
                if st.button("◀"):
                    st.session_state.page_idx = max(0, st.session_state.page_idx - 1)
                    # 페이지 이동 시 텍스트 갱신
                    st.session_state.editor_text_content = extract_text_unified(main_file, st.session_state.page_idx)
                    st.rerun()
            with c_next:
                if st.button("▶"):
                    st.session_state.page_idx = min(total_pages - 1, st.session_state.page_idx + 1)
                    st.session_state.editor_text_content = extract_text_unified(main_file, st.session_state.page_idx)
                    st.rerun()
            with c_jump:
                target = st.number_input("이동", 1, total_pages, st.session_state.page_idx+1)
                if target-1 != st.session_state.page_idx:
                    st.session_state.page_idx = target-1
                    st.session_state.editor_text_content = extract_text_unified(main_file, st.session_state.page_idx)
                    st.rerun()
            
            # 쪽수 오프셋 (PDF 페이지와 교과서 쪽수 맞춤)
            st.session_state.start_page_offset = st.number_input("시작 쪽수 오프셋", value=st.session_state.start_page_offset)
            page_str = str(st.session_state.page_idx + st.session_state.start_page_offset)
            st.caption(f"현재 PDF {st.session_state.page_idx+1}페이지 ➡️ 교과서 {page_str}쪽")
        else:
            # 이미지 파일일 경우 쪽수 직접 입력
            page_str = st.text_input("쪽수", value="1")
    else:
        st.info("파일 없음 (직접 입력 모드)")
        page_str = st.text_input("저장될 쪽수", value="1")

# [우측] 텍스트 입력 및 분석 (상시 노출)
with col_input:
    st.info("📝 분석 내용 입력 (수정 가능)")
    
    # [핵심] 텍스트창 상태 고정 (key 사용 + session_state 동기화)
    # 파일이 없어도 항상 표시되며, 새로고침 시에도 내용이 유지됨
    txt_val = st.text_area(
        "분석할 텍스트를 입력하세요.", 
        value=st.session_state.editor_text_content,
        height=500,
        key="main_editor_area"
    )
    
    # 사용자가 입력한 내용 업데이트
    if txt_val != st.session_state.editor_text_content:
        st.session_state.editor_text_content = txt_val

    # 분석 실행 버튼
    if st.button("🚀 분석 실행", type="primary", use_container_width=True):
        if not txt_val.strip():
            st.warning("분석할 텍스트가 없습니다.")
        else:
            with st.spinner("AI가 생각하며 분석 중입니다..."):
                # 텍스트가 너무 적으면 이미지 Vision 사용 (하이브리드 분석)
                # 이미지는 파일이 있고 텍스트가 10자 미만일 때만 전송
                s_img = img_bytes if (main_file and len(txt_val) < 10) else None
                
                # 분석 호출
                res = get_analysis_hybrid(txt_val, s_img, sheet_data, MODE_KEY)
                
                # 결과 가공 (빈도 계산 등)
                proc = []
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                
                cnts = Counter([r.get('original_word', '미상') for r in res])
                seen = set()
                
                for r in res:
                    # [KeyError 방지] 안전하게 값 가져오기
                    root = r.get('root_word', '')
                    
                    raw_origin = r.get('origin', '혼')
                    raw_pos = r.get('pos', '명사')
                    raw_original = r.get('original_word', '미상')
                    
                    if root not in seen:
                        proc.append({
                            "delete_check": False,
                            "count": f"{cnts[raw_original]}회",
                            "original_word": raw_original,
                            "root_word": root,
                            "origin": om.get(raw_origin, raw_origin),
                            "pos": pm.get(raw_pos, raw_pos)
                        })
                        seen.add(root)
                
                st.session_state.analysis_result = proc

# [결과 화면] 탭 이동 없이 바로 아래 표시
if st.session_state.analysis_result:
    st.divider()
    st.subheader("2. 분석 결과 확인")
    
    df_r = pd.DataFrame(st.session_state.analysis_result)
    
    # 데이터 에디터 (수정 가능)
    edited = st.data_editor(
        df_r,
        column_config={
            "delete_check": st.column_config.CheckboxColumn("삭제"),
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사"]),
        },
        num_rows="dynamic", # 행 추가 허용 (엔터로 추가)
        use_container_width=True
    )
    
    c1, c2, c3 = st.columns(3)
    
    # [버튼 1] 체크 항목 삭제 (학습 포함)
    with c1:
        if st.button("⛔ 체크 항목 삭제"):
            dels = edited[edited['delete_check'] == True]
            if not dels.empty and sheet:
                # 삭제 규칙 학습 전송 ('의미' 제외)
                logs = [[
                    datetime.now().isoformat(), 
                    r['original_word'], 
                    r['root_word'], 
                    "", "", 'delete', 
                    'User Delete'
                ] for _, r in dels.iterrows()]
                
                send_data_with_retry(sheet, logs, True)
                st.toast("삭제 규칙이 학습되었습니다.", icon="🗑️")
                
                # 화면 갱신 (삭제된 행 제외)
                leftover = edited[edited['delete_check'] == False].to_dict('records')
                st.session_state.analysis_result = leftover
                st.rerun()

    # [버튼 2] 저장하고 다음 페이지로
    with c2:
        if st.button("💾 저장하고 다음 (▶)"):
            if save_logic(edited, page_str, sheet, txt_val):
                st.toast("저장되었습니다!")
                
                # PDF 모드면 다음 페이지로 자동 이동
                if main_file and "pdf" in main_file.type and st.session_state.page_idx < total_pages - 1:
                    st.session_state.page_idx += 1
                    # 다음 페이지 텍스트 로드
                    nx_txt = extract_text_unified(main_file, st.session_state.page_idx)
                    st.session_state.editor_text_content = nx_txt
                    st.session_state.analysis_result = [] # 결과창 초기화
                    time.sleep(0.5)
                    st.rerun()
                elif main_file and "pdf" in main_file.type:
                    st.success("마지막 페이지입니다.")
                else:
                    st.success("저장 완료. (이미지/직접입력 모드는 다음 페이지가 없습니다)")

    # [버튼 3] 저장만 하기
    with c3:
        if st.button("💾 저장만 하기"):
            if save_logic(edited, page_str, sheet, txt_val):
                st.success("저장되었습니다.")

# [하단] 전체 누적 데이터 표시 (GP9 기능)
if st.session_state.master_df is not None:
    st.markdown("---")
    st.subheader("📊 전체 누적 데이터")
    
    st.dataframe(st.session_state.master_df, use_container_width=True)
    
    # 엑셀 다운로드 버튼
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w: 
        st.session_state.master_df.to_excel(w, index=False)
    
    st.download_button(
        label="📥 전체 엑셀 다운로드", 
        data=buf.getvalue(), 
        file_name="final_result.xlsx", 
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )