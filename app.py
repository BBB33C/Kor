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
        API_KEY = "" # 로컬 테스트용
except:
    API_KEY = ""

MODEL_NAME = "gemini-2.0-flash-exp"
SHEET_NAME = "Korean_DB"
TRUST_THRESHOLD = 3 

# [CSS] 텍스트창 진한 회색(#262730) 배경 + 흰색 글씨 (눈 보호 모드)
st.markdown("""
    <style>
        /* 메인 텍스트 입력창 스타일링 */
        .stTextArea textarea { 
            font-size: 16px !important; 
            line-height: 1.6 !important; 
            font-family: 'Malgun Gothic', sans-serif !important; 
            background-color: #262730 !important; /* 진한 회색 배경 */
            color: #ffffff !important; /* 흰색 글씨 */
            border: 1px solid #4a4a4a !important; 
            font-weight: 400 !important;
        }
        /* 입력창 포커스 시 테두리 */
        .stTextArea textarea:focus {
            border: 1px solid #ff4b4b !important;
        }
        
        .stDataFrame { border: 1px solid #ddd; }
        .block-container { padding-top: 2rem; }
        
        /* 확장 패널 스타일 */
        div[data-testid="stExpander"] details summary p {
            font-size: 1.05rem;
            font-weight: 600;
        }
        
        /* 버튼 스타일 */
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
            # 시트가 없으면 자동으로 생성 (GP9 기능)
            st.warning(f"⚠️ '{target_sheet_name}' 시트가 없어 새로 생성합니다.")
            sheet = spreadsheet.add_worksheet(title=target_sheet_name, rows=1000, cols=20)
            # GP9 헤더 양식 유지
            sheet.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context"])
            
        data = sheet.get_all_records()
        return sheet, data
    except Exception as e:
        # 연결 실패 시 조용히 넘어가거나 로그 출력
        return None, []

def send_data_with_retry(sheet_obj, data, is_multiple=False):
    """데이터 전송 실패 시 3회 재시도 로직 (GP9 안전장치)"""
    if not sheet_obj: return False
    
    max_retries = 3
    for attempt in range(max_retries):
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
            if attempt < max_retries - 1:
                time.sleep(1) # 잠시 대기 후 재시도
                continue
            else:
                st.error(f"❌ 데이터 전송 최종 실패: {str(e)}")
                return False
    return False

def save_backup_to_cloud(mode_key, df):
    """[핵심] 현재 작업 데이터를 클라우드 백업 시트에 통째로 저장 (GP9 기능)"""
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
        # 백업 실패는 치명적이지 않으므로 콘솔 로그만 남김
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
# [3] AI 엔진 (1~7단계 로직 + GP9 OCR + '의미' 삭제)
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
            # 의미(meaning) 열 삭제됨 -> 제외하고 규칙 생성
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
    
    # GP9의 상세 OCR 프롬프트 복원
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

def split_text_smartly(text, chunk_size=1000):
    """[GP9 기능] 긴 텍스트 분할 처리 (복원됨)"""
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
    
    # 긴 텍스트 처리 로직 (GP9)
    if image_bytes:
        # 이미지는 분할 없이 한 번에 전송 (Vision API 제한 고려)
        full_prompt = f"{prompt}\n\n(이미지 OCR 결과 참고)"
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
            except: return []
        return []
    else:
        # 텍스트는 긴 경우 분할 처리
        chunks = split_text_smartly(text)
        all_results = []
        for chunk in chunks:
            full_prompt = f"{prompt}\n\n[분석할 텍스트]:\n{chunk}"
            res_text = api_call_direct(full_prompt)
            if res_text:
                try:
                    match = re.search(r'\[.*\]', res_text, re.DOTALL)
                    if match: 
                        all_results.extend(json.loads(match.group()))
                    else:
                        s = res_text.find('[')
                        e = res_text.rfind(']') + 1
                        if s != -1 and e != -1: 
                            all_results.extend(json.loads(res_text[s:e]))
                except: pass
            time.sleep(0.1) # API 부하 방지
        return all_results

# =========================================================
# [4] 파일 처리 및 텍스트 추출 (GP9 로직 100% 복구)
# =========================================================
def extract_text_unified(file_obj, page_idx):
    """GP9의 정밀 추출 로직 (Crop -> OCR Fallback)"""
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
                        # GP9는 헤더/푸터 제외를 위해 crop을 시도했음
                        width, height = page.width, page.height
                        try:
                            # 상단 5%, 하단 10% 제외
                            crop_box = (0, height * 0.05, width, height * 0.9)
                            cropped = page.crop(crop_box)
                            text = cropped.extract_text()
                        except:
                            text = page.extract_text()
            except: pass
        
        # 2. 텍스트가 없으면 fitz 시도 (PyMuPDF)
        if not text and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_idx < len(doc):
                    text = doc[page_idx].get_text()
            except: pass
        
        # 3. [핵심 복구] 텍스트가 너무 적으면 이미지로 변환해 Vision OCR (스캔본 대응)
        if (not text or len(text.strip()) < 30) and FITZ_AVAILABLE:
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
    # 1. 학습 전송
    if sheet_obj:
        logs = []
        for _, row in edited_df.iterrows():
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

    # 2. 마스터 업데이트
    valid_rows = edited_df[edited_df['delete_check'] == False].copy()
    
    def parse_count(val):
        try: return int(str(val).replace('회', '').strip())
        except: return 1
    valid_rows['numeric_count'] = valid_rows['count'].apply(parse_count)
    
    # '의미' 없이 그룹화
    aggregated = valid_rows.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({
        'numeric_count': 'sum', 
        'original_word': lambda x: ', '.join(x.unique())
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
        
        # '의미' 없이 자료+구분 기준 식별
        mask = (master['자료'] == root) & (master['구분'] == origin_val)
        
        if mask.any():
            idx = master[mask].index[0]
            filled_cols = [c for c in master.columns if '쪽수' in c and pd.notna(master.at[idx, c])]
            next_col = f"쪽수{len(filled_cols) + 1}"
            
            if next_col not in master.columns:
                master[next_col] = None 
            
            master.at[idx, next_col] = val_to_save
        else:
            new_rows_list.append({
                '구분': origin_val,
                '자료': root,
                '출연횟수': 0, 
                '쪽수1': val_to_save
            })
            
    if new_rows_list:
        master = pd.concat([master, pd.DataFrame(new_rows_list)], ignore_index=True)
    
    master['출연횟수'] = master.apply(calc_freq, axis=1)
    
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    master['sort_key'] = master['구분'].map(sort_map).fillna(5)
    master = master.sort_values(['sort_key', '자료']).drop('sort_key', axis=1)
    
    st.session_state.master_df = master
    
    # 자동 백업 실행
    if save_backup_to_cloud(st.session_state.last_mode, master):
        pass 
    else:
        st.toast("⚠️ 로컬 저장은 되었으나, 클라우드 백업에 실패했습니다.", icon="☁️")
        
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

# [사이드바] 설정 및 도구
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
    if sheet: 
        st.caption(f"✅ 학습 데이터 연결됨: {len(sheet_data)}건")
    else: 
        st.error("❌ 구글 시트 연결 실패")
    
    st.markdown("---")
    st.header("📂 이어하기")
    
    up_excel = st.file_uploader("엑셀 파일 선택", type=['xlsx'])
    
    if up_excel and up_excel.name != st.session_state.last_uploaded_file_name:
        if st.button("병합하기"):
            try:
                loaded = pd.read_excel(up_excel)
                if st.session_state.master_df is not None:
                    # 의미 열 없이 병합
                    cols = ['자료', '구분']
                    m = pd.concat([st.session_state.master_df, loaded]).drop_duplicates(subset=cols, keep='first')
                    st.session_state.master_df = m
                else:
                    st.session_state.master_df = loaded
                
                st.session_state.last_uploaded_file_name = up_excel.name
                st.success("데이터 병합 완료!")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.error(f"엑셀 로드 오류: {e}")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("☁️ 강제 백업"): 
            if save_backup_to_cloud(MODE_KEY, st.session_state.master_df): st.toast("백업 성공")
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
    # [GP9 기능] 수동 추가 폼 (의미 열 삭제 반영)
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
    st.caption("🔍 이력 검색")
    search_q = st.text_input("단어 검색", placeholder="예: 사랑")
    if search_q and sheet_data:
        found = [row for row in sheet_data if search_q in str(row.get('root_word')) or search_q in str(row.get('original_word'))]
        if found:
            for f in found[-3:]: st.text(f"[{f.get('action')}] {f.get('root_word')} ({f.get('origin')})")

st.subheader("1. 분석 자료 입력")
main_file = st.file_uploader("PDF/이미지 파일 (선택)", type=['pdf', 'png', 'jpg'])

# [오류 해결] AttributeError: ... has no attribute 'id' 방지
# Streamlit의 file_uploader 객체는 id 속성이 없을 수 있으므로 name과 size로 식별
if main_file:
    file_id = f"{main_file.name}_{main_file.size}"
    if st.session_state.file_hash != file_id:
        st.session_state.file_hash = file_id
        st.session_state.page_idx = 0
        st.session_state.analysis_result = []
        # 새 파일이면 텍스트 추출하여 상태 업데이트
        st.session_state.editor_text_content = extract_text_unified(main_file, 0)
        st.rerun()

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
        if img_bytes: st.image(img_bytes, use_container_width=True)
        else: st.warning("미리보기를 생성할 수 없습니다.")
        
        if "pdf" in main_file.type:
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
            
            st.session_state.start_page_offset = st.number_input("시작 쪽수 오프셋", value=st.session_state.start_page_offset)
            page_str = str(st.session_state.page_idx + st.session_state.start_page_offset)
            st.caption(f"현재 PDF {st.session_state.page_idx+1}페이지 ➡️ 교과서 {page_str}쪽")
        else:
            # 이미지 파일일 경우 쪽수 직접 입력
            page_str = st.text_input("쪽수", value="1")
    else:
        st.info("파일 없음 (직접 입력 모드)")
        page_str = st.text_input("저장될 쪽수", value="1")

with col_input:
    st.info("📝 분석 내용 입력 (수정 가능)")
    txt_val = st.text_area(
        "분석할 텍스트를 입력하세요.", 
        value=st.session_state.editor_text_content,
        height=500,
        key="main_editor_area"
    )
    if txt_val != st.session_state.editor_text_content:
        st.session_state.editor_text_content = txt_val

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
                
                # [오류 해결] KeyError 방지를 위해 .get() 사용하여 안전하게 카운팅
                all_originals = [r.get('original_word', '미상') for r in res]
                cnts = Counter(all_originals)
                seen = set()
                
                for r in res:
                    # [오류 해결] KeyError 방지를 위해 .get() 사용
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

if st.session_state.analysis_result:
    st.divider()
    st.subheader("2. 분석 결과 확인")
    df_r = pd.DataFrame(st.session_state.analysis_result)
    
    # '의미' 열 제거됨
    edited = st.data_editor(
        df_r,
        column_config={
            "delete_check": st.column_config.CheckboxColumn("삭제"),
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사"])
        },
        num_rows="dynamic", # 행 추가 허용 (엔터로 추가)
        use_container_width=True
    )
    
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("⛔ 체크 삭제"):
            dels = edited[edited['delete_check']==True]
            if not dels.empty and sheet:
                # 삭제 규칙 학습 전송 ('의미' 제외)
                l = [[datetime.now().isoformat(), r['original_word'], r['root_word'], "", "", 'delete', 'User'] for _, r in dels.iterrows()]
                
                send_data_with_retry(sheet, l, True)
                st.toast("삭제 규칙이 학습되었습니다.", icon="🗑️")
                
                # 화면 갱신 (삭제된 행 제외)
                leftover = edited[edited['delete_check'] == False].to_dict('records')
                st.session_state.analysis_result = leftover
                st.rerun()

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

    with c3:
        if st.button("💾 저장만 하기"):
            if save_logic(edited, page_str, sheet, txt_val):
                st.success("저장되었습니다.")

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