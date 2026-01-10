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

# =========================================================
# [0] 라이브러리 임포트 및 상태 체크
# =========================================================
# PDF 처리 라이브러리 확인
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

# 구글 시트 라이브러리 확인
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# 페이지 기본 설정
st.set_page_config(
    page_title="국어활동 AI 분석기 (Ultimate)", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# =========================================================
# [1] API 및 기본 설정
# =========================================================
# API 키 로드 (secrets.toml 우선, 없으면 로컬 변수)
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = "" # 로컬 테스트용 키 입력
except:
    API_KEY = ""

MODEL_NAME = "gemini-2.0-flash-exp" # 최신 모델 사용
SHEET_NAME = "Korean_DB" # 구글 시트 이름

# 스타일 설정 (가독성 최적화 및 입력창 고정)
st.markdown("""
    <style>
        .stTextArea textarea { 
            font-size: 15px !important; 
            line-height: 1.6 !important; 
            font-family: 'Malgun Gothic', sans-serif !important; 
            border: 1px solid #ddd !important;
            background-color: #fcfcfc;
        }
        .stDataFrame { border: 1px solid #ccc; }
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
# [2] 구글 시트 & 백업 시스템 (_GP9 핵심 기능 복구)
# =========================================================
@st.cache_resource
def get_google_sheet_client():
    """구글 시트 인증 클라이언트 생성"""
    if not GSPREAD_AVAILABLE: return None
    try:
        if "gcp_service_account" not in st.secrets: return None
        
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        
        # 개행 문자 처리 (TOML 파일 특성상 필요)
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
            worksheet = spreadsheet.worksheet(target_sheet_name)
        except: 
            # 시트가 없으면 생성
            worksheet = spreadsheet.add_worksheet(title=target_sheet_name, rows=1000, cols=20)
            # 헤더 추가
            worksheet.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context", "meaning"])
            
        return worksheet, worksheet.get_all_records()
    except Exception as e:
        # 연결 실패 시 조용히 넘어가거나 로그 출력
        # st.error(f"시트 로드 에러: {e}") 
        return None, []

def send_data_with_retry(sheet_obj, data, is_multiple=False):
    """데이터 전송 실패 시 재시도 로직 (안전장치)"""
    if not sheet_obj: return False
    
    max_retries = 3
    for i in range(max_retries):
        try:
            if is_multiple:
                # 2차원 리스트 전송 (대량 학습)
                clean_data = [[str(item) for item in row] for row in data]
                sheet_obj.append_rows(clean_data)
            else:
                # 1차원 리스트 전송 (단건 학습)
                clean_data = [str(item) for item in data]
                sheet_obj.append_row(clean_data)
            return True
        except Exception as e:
            time.sleep(1) # 1초 대기 후 재시도
            if i == max_retries - 1:
                st.error(f"데이터 전송 실패: {e}")
    return False

def save_backup_to_cloud(mode_key, df):
    """현재 작업 내용을 클라우드 백업 시트에 통째로 저장 (안전장치)"""
    client = get_google_sheet_client()
    if not client or df is None or df.empty: return False
    
    backup_tab_name = f"Backup_{'South' if mode_key=='SOUTH' else 'North'}"
    
    try:
        sh = client.open(SHEET_NAME)
        try: 
            ws = sh.worksheet(backup_tab_name)
            ws.clear() # 기존 백업 삭제
        except: 
            ws = sh.add_worksheet(title=backup_tab_name, rows=1000, cols=20)
        
        # DataFrame 전체를 문자열로 변환하여 업로드 (NaN 방지)
        df_str = df.fillna("").astype(str)
        payload = [df_str.columns.tolist()] + df_str.values.tolist()
        ws.update(payload)
        return True
    except Exception as e: 
        print(f"백업 실패: {e}")
        return False

def load_backup_from_cloud(mode_key):
    """클라우드에서 백업 데이터를 불러옴"""
    client = get_google_sheet_client()
    if not client: return None
    
    backup_tab_name = f"Backup_{'South' if mode_key=='SOUTH' else 'North'}"
    try:
        sh = client.open(SHEET_NAME)
        ws = sh.worksheet(backup_tab_name)
        data = ws.get_all_records()
        return pd.DataFrame(data) if data else None
    except: return None

# =========================================================
# [3] AI 엔진 (1~7단계 요구사항 + 동음이의어 + CoT)
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    """[요구사항 7] 시트 데이터를 기반으로 학습 규칙 프롬프트 생성"""
    if not sheet_data: return ""
    
    rules = []
    # 최신 학습된 내용 우선 반영 (최신 50개)
    # _GP9.py의 action 컬럼(add, modify, delete) 활용
    for row in sheet_data[-50:]:
        if row.get('action') == 'delete':
            rules.append(f"- [삭제 규칙]: '{row.get('original_word')}'는 분석 결과에서 제외하세요.")
        elif row.get('action') in ['add', 'modify']:
            meaning_info = f", 의미:'{row.get('meaning')}'" if row.get('meaning') else ""
            rules.append(f"- [고정 규칙]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}'{meaning_info}, 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    
    if rules:
        return "\n[사용자 학습 규칙 (최우선 적용)]:\n" + "\n".join(rules) + "\n"
    return ""

def api_call_direct(prompt, image_bytes=None):
    """Gemini API 호출 (Text Only 또는 Vision)"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    parts = [{"text": prompt}]
    
    if image_bytes:
        # Vision 모드: 이미지 데이터를 base64로 인코딩하여 전송
        b64_data = base64.b64encode(image_bytes).decode('utf-8')
        parts.append({"inline_data": {"mime_type": "image/png", "data": b64_data}})
    
    data = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1, # 창의성 억제 (정확도 우선)
            "maxOutputTokens": 8192
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            result = response.json()
            if 'candidates' in result:
                return result['candidates'][0]['content']['parts'][0]['text']
        else:
            # 에러 발생 시 로그 출력 (디버깅용)
            print(f"API Error: {response.status_code} - {response.text}")
        return None
    except Exception as e:
        print(f"Connection Error: {e}")
        return None

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    """
    [요구사항 5, 6, 7 반영] CoT + 하다 처리 + 동음이의어 + 학습 반영 통합 분석 함수
    """
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    mode_desc = "대한민국 표준어" if mode_key == "SOUTH" else "북한 문화어(두음법칙 미적용)"
    
    # [CoT 프롬프트 설계]
    prompt = f"""
    당신은 '{mode_desc}' 형태소 분석 및 국어사전 편찬 전문가입니다.
    주어진 텍스트(또는 이미지 속 텍스트)를 분석하여 JSON 형식으로 출력하세요.
    
    {learning_prompt}
    
    [분석 단계 (Chain of Thought)]
    1. **문맥 파악**: 텍스트의 맥락을 읽고 '{mode_desc}' 규칙을 적용하세요.
    2. **형태소 분리**: 조사(은/는/이/가/을/를/에/에게 등)와 어미를 철저히 분리하여 제거합니다.
       - 예: '친구를' -> '친구'
    3. **'하다' 용언 처리 (중요)**: '명사+하다' 형태는 기계적으로 나누지 말고 문맥을 봅니다.
       - 동작성이 강하면 동사 (예: '공부하다', '생각하다')
       - 명사성이 강하면 명사 (예: '사랑', '감사')
       - 문맥상 더 자연스러운 원형을 선택하세요.
    4. **동음이의어 구분**: 같은 단어라도 뜻이 다르면 'meaning' 필드에 간략히 적어 구분합니다.
       - 예: 배(과일), 배(선박), 배(신체)
    5. **품사 필터링**: 명사, 동사, 형용사, 부사, 관형사, 대명사만 남깁니다.
    6. **출력**: 아래 JSON 포맷을 엄수하세요. (코드 블록 없이 순수 JSON만 출력하면 더 좋습니다)

    [JSON 예시]
    [
        {{"original_word": "배를", "root_word": "배", "meaning": "과일", "origin": "고", "pos": "명사"}},
        {{"original_word": "공부했다", "root_word": "공부하다", "meaning": "학습", "origin": "한", "pos": "동사"}}
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
# [4] 파일 처리 및 텍스트 추출 (_GP9 복원)
# =========================================================
def extract_text_unified(file_obj, page_idx):
    """PDF/이미지 통합 텍스트 추출기 (폴백 로직 포함)"""
    file_type = file_obj.type
    
    # 이미지는 Vision으로 처리하므로 텍스트 추출 안 함 (빈 문자열 반환)
    if "image" in file_type:
        return "" 
        
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
        
        # 3. 텍스트가 여전히 없거나 너무 짧으면(이미지형 PDF), Vision OCR 유도를 위해 빈 값 반환
        # (메인 로직에서 텍스트가 비어있으면 이미지를 전송하도록 되어 있음)
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
# [5] 데이터 저장 로직 (_GP9 핵심 - 동적 컬럼 & 백업)
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
    # 1. 구글 시트로 학습 데이터 전송 (수정된 내용 학습)
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
                    context_text[:50], # 문맥 일부 저장
                    row.get('meaning', '') # 의미 추가
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
    
    # 의미 컬럼이 없으면 생성
    if 'meaning' not in valid_rows.columns: valid_rows['meaning'] = ''
    
    # 같은 단어끼리 묶기 (Root + Origin + Pos + Meaning 기준)
    aggregated = valid_rows.groupby(['root_word', 'origin', 'pos', 'meaning'], as_index=False).agg({
        'numeric_count': 'sum', 
        'original_word': lambda x: ', '.join(x.unique())
    })
    
    # 마스터 DF 초기화
    if st.session_state.master_df is None:
        st.session_state.master_df = pd.DataFrame(columns=['구분', '자료', '의미', '출연횟수', '쪽수1'])
    
    master = st.session_state.master_df
    
    # 의미 컬럼 보장
    if '의미' not in master.columns: master.insert(2, '의미', '')
    
    # 쪽수 컬럼 데이터 타입 정리 (문자열 등 허용)
    for c in master.columns:
        if '쪽수' in c: master[c] = master[c].astype(object)

    new_rows_list = []
    
    for _, item in aggregated.iterrows():
        root = item['root_word']
        origin_val = clean_val(item['origin'])
        meaning_val = item['meaning']
        cnt = item['numeric_count']
        
        # 저장할 값 포맷: "쪽수_빈도" (빈도가 1보다 크면)
        val_to_save = f"{page_str}_{cnt}" if cnt > 1 else page_str
        
        # 기존 데이터에 있는지 확인 (자료 + 구분 + 의미)
        mask = (master['자료'] == root) & (master['구분'] == origin_val) & (master['의미'] == meaning_val)
        
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
                '의미': meaning_val,
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
        pass # 백업 성공
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
# [핵심] 입력 텍스트 상태 보존용
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
                    # 의미 컬럼이 없을 경우 대비
                    cols = ['자료', '구분']
                    if '의미' in loaded.columns: cols.append('의미')
                    
                    # keep='last' or 'first': 기존 작업을 우선할지 파일 내용을 우선할지
                    # 안전하게 합치기 위해 concat 후 중복 제거
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
            m = st.text_input("의미 (선택, 동음이의어)")
            org = st.selectbox("분류", ["고","한","외","혼"])
            p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사"])
            
            if st.form_submit_button("추가 및 학습"):
                if o and r and sheet:
                    # 학습 전송
                    send_data_with_retry(sheet, [
                        datetime.now().isoformat(), o, r, org, p, 'add', '수동추가', m
                    ])
                    st.toast(f"'{r}' 단어가 추가되었습니다.")

    # [GP9 기능] 이력 검색
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

# 2단 컬럼 레이아웃 (뷰어 | 입력창)
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
                target = st.number_input("페이지 이동", 1, total_pages, st.session_state.page_idx+1)
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
                
                cnts = Counter([r['original_word'] for r in res])
                seen = set()
                
                for r in res:
                    root = r['root_word']
                    meaning = r.get('meaning', '')
                    # 고유 키 생성 (단어_의미)
                    ukey = f"{root}_{meaning}"
                    
                    if ukey not in seen:
                        proc.append({
                            "delete_check": False,
                            "count": f"{cnts[r['original_word']]}회",
                            "original_word": r['original_word'],
                            "root_word": root,
                            "meaning": meaning,
                            "origin": om.get(r['origin'], r['origin']),
                            "pos": pm.get(r['pos'], r['pos'])
                        })
                        seen.add(ukey)
                
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
            "count": st.column_config.TextColumn("빈도", disabled=True),
            "original_word": st.column_config.TextColumn("원본"),
            "root_word": st.column_config.TextColumn("원형"),
            "meaning": st.column_config.TextColumn("의미(동음이의어)"),
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사"]),
        },
        num_rows="dynamic", # 행 추가 허용 (엔터로 추가)
        use_container_width=True
    )
    
    # 하단 액션 버튼들
    c1, c2, c3 = st.columns(3)
    
    # [버튼 1] 체크 항목 삭제 (학습 포함)
    with c1:
        if st.button("⛔ 체크 항목 삭제"):
            dels = edited[edited['delete_check'] == True]
            if not dels.empty and sheet:
                # 삭제 규칙 학습 전송
                logs = [[
                    datetime.now().isoformat(), 
                    r['original_word'], 
                    r['root_word'], 
                    "", "", 'delete', 
                    'User Delete', ''
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

# [하단] 전체 누적 데이터 표시
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