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

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# =========================================================
# ⚙️ 0. 기본 설정
# =========================================================
st.set_page_config(page_title="국어활동 AI 분석기 (Integrated)", page_icon="📚", layout="wide")

# API KEY 로드 (secrets 우선, 없으면 하드코딩 - 보안 주의)
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = "YOUR_API_KEY_HERE" # 직접 입력 필요 시 여기에
except:
    API_KEY = "YOUR_API_KEY_HERE"

MODEL_NAME = "gemini-1.5-flash" # 속도와 성능 균형을 위해 1.5 Flash 권장 (또는 2.0-flash-exp)
SHEET_NAME = "Korean_DB"
TRUST_THRESHOLD = 3

# 스타일 커스터마이징 (가독성)
st.markdown("""
    <style>
        .stTextArea textarea { font-size: 14px; line-height: 1.6; }
        .stDataFrame { border: 1px solid #ddd; }
    </style>
""", unsafe_allow_html=True)

# =========================================================
# 🔐 1. 구글 시트 & 백업 로직 (_GP9.py 계승)
# =========================================================
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
    client = get_google_sheet_client()
    if not client: return None, []
    target_sheet_name = "South_Korea" if mode_key == "SOUTH" else "North_Korea"
    try:
        spreadsheet = client.open(SHEET_NAME)
        sheet = spreadsheet.worksheet(target_sheet_name)
        data = sheet.get_all_records()
        return sheet, data
    except Exception as e:
        # st.warning(f"시트 연결 실패 ('{target_sheet_name}'): {e}") # 사용자 혼란 방지 위해 로그 최소화
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
        except Exception:
            time.sleep(1)
    return False

def save_backup_to_cloud(mode_key, df):
    client = get_google_sheet_client()
    if not client or df is None or df.empty: return False
    
    backup_name = f"Backup_{'South' if mode_key == 'SOUTH' else 'North'}"
    try:
        sh = client.open(SHEET_NAME)
        try: ws = sh.worksheet(backup_name); ws.clear()
        except: ws = sh.add_worksheet(title=backup_name, rows=1000, cols=20)
        
        # NaN 처리
        df_str = df.fillna("").astype(str)
        data = [df_str.columns.values.tolist()] + df_str.values.tolist()
        ws.update(data)
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
# 🧠 2. AI 분석 엔진 (CoT + 7단계 요구사항 적용)
# =========================================================
def generate_system_prompt(sheet_data, mode_key):
    """
    [Req 5, 6, 7] CoT 프롬프트 + 학습 데이터 반영 + 하다 처리
    """
    mode_desc = "대한민국 표준어" if mode_key == "SOUTH" else "북한 문화어(두음법칙 미적용)"
    
    # 학습 규칙 추출 (최근 30개만 - 토큰 절약)
    rules = []
    if sheet_data:
        for row in sheet_data[-30:]:
            if row.get('action') == 'delete':
                rules.append(f"- [제외]: '{row.get('original_word')}'")
            elif row.get('action') in ['add', 'modify']:
                rules.append(f"- [고정]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}', 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    
    rules_text = "\n".join(rules) if rules else "없음"

    prompt = f"""
    당신은 '{mode_desc}' 국어사전 편찬 전문가입니다.
    주어진 텍스트를 분석하여 JSON으로 출력하세요.

    [학습된 사용자 규칙 (최우선 적용)]
    {rules_text}

    [분석 단계 (Chain of Thought)]
    1. **문맥 파악**: '{mode_desc}' 문맥을 고려합니다.
    2. **형태소 분리**: 조사(은/는/이/가/을/를 등)와 어미를 철저히 분리/제거합니다.
    3. **'하다' 용언 판단 (Req 6)**: '명사+하다'는 기계적으로 나누지 말고 문맥을 봅니다.
       - 동작성 강함 -> 동사 (예: 공부하다)
       - 명사성 강함 -> 명사 (예: 사랑)
       - 문맥에 더 자연스러운 쪽을 선택하세요.
    4. **품사 필터링**: 명사, 동사, 형용사, 부사, 관형사, 대명사만 남깁니다. (의존명사, 수사 제외)
    5. **출력**: 아래 JSON 포맷을 엄수하세요.

    [JSON 예시]
    [
        {{"original_word": "공부했다", "root_word": "공부하다", "origin": "한", "pos": "동사"}},
        {{"original_word": "학교에", "root_word": "학교", "origin": "한", "pos": "명사"}}
    ]
    (origin: 고, 한, 외, 혼 / pos: 명사, 동사, 형용사, 부사, 관형사, 대명사)
    """
    return prompt

def api_call_direct(prompt, image_bytes=None):
    """Gemini API 호출 (Text or Vision)"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    parts = [{"text": prompt}]
    if image_bytes:
        b64_img = base64.b64encode(image_bytes).decode('utf-8')
        parts.append({"inline_data": {"mime_type": "image/png", "data": b64_img}})
    
    data = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192}
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()
        return None
    except: return None

def analyze_hybrid(text, image_bytes, sheet_data, mode_key):
    """이미지/텍스트 하이브리드 분석"""
    sys_prompt = generate_system_prompt(sheet_data, mode_key)
    
    user_msg = "위 텍스트를 분석하세요."
    if image_bytes:
        user_msg = "위 이미지의 텍스트를 읽고(OCR), 위 분석 규칙에 따라 분석 결과를 JSON으로 주세요."
    else:
        user_msg = f"[분석할 텍스트]:\n{text}"

    final_prompt = f"{sys_prompt}\n\n{user_msg}"
    
    res = api_call_direct(final_prompt, image_bytes)
    
    if res and 'candidates' in res:
        try:
            content = res['candidates'][0]['content']['parts'][0]['text']
            # JSON 파싱
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                return json.loads(match.group())
            # 백업 파싱
            s = content.find('[')
            e = content.rfind(']') + 1
            if s != -1 and e != -1:
                return json.loads(content[s:e])
        except: return []
    return []

# =========================================================
# 📂 3. 파일 처리 (PDF/이미지) - _GP9.py 기능 복원
# =========================================================
def extract_text_unified(file_obj, page_idx):
    """_GP9.py의 강력한 추출 로직 유지"""
    if "image" in file_obj.type:
        return "" # 이미지는 Vision API가 처리하므로 텍스트 추출 불필요 (빈 문자열)
    
    elif "pdf" in file_obj.type:
        text = ""
        # 1. pdfplumber 시도
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(file_obj) as pdf:
                    if page_idx < len(pdf.pages):
                        text = pdf.pages[page_idx].extract_text()
            except: pass
        
        # 2. 텍스트가 없으면 PyMuPDF(fitz) 시도
        if not text and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
                if page_idx < len(doc):
                    text = doc[page_idx].get_text()
            except: pass
            
        return text if text else "텍스트를 추출할 수 없습니다. (이미지 분석 권장)"
    return ""

def get_page_image_bytes(file_obj, page_idx):
    """뷰어용 이미지 생성"""
    if "image" in file_obj.type:
        return file_obj.getvalue()
    elif "pdf" in file_obj.type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_obj.getvalue(), filetype="pdf")
            if page_idx < len(doc):
                pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                return pix.tobytes("png")
        except: pass
    return None

# =========================================================
# 🖥️ 4. 메인 UI (Streamlit Layout)
# =========================================================
# 상태 초기화
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'file_hash' not in st.session_state: st.session_state.file_hash = None
if 'start_page_offset' not in st.session_state: st.session_state.start_page_offset = 1

st.title("📚 국어활동 AI 분석기 (Pro)")

# --- 사이드바: 설정 및 파일 로드 ---
with st.sidebar:
    st.header("⚙️ 설정")
    mode = st.radio("분석 모드", ["🇰🇷 표준어", "🇰🇵 문화어"])
    MODE_KEY = "SOUTH" if "표준어" in mode else "NORTH"
    
    # 모드 변경 시 초기화 방지 로직 (Req 2)
    if 'last_mode' not in st.session_state: st.session_state.last_mode = MODE_KEY
    if st.session_state.last_mode != MODE_KEY:
        st.session_state.last_mode = MODE_KEY
        st.toast(f"모드가 {mode}(으)로 변경되었습니다.")
    
    sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)
    if sheet: st.caption(f"✅ 학습 데이터: {len(sheet_data)}건")
    else: st.error("❌ 구글 시트 연결 안됨")

    st.markdown("---")
    st.header("📂 1. 이어하기 & 백업")
    
    # [Req 1] 엑셀 이어하기 (Merge Logic)
    uploaded_excel = st.file_uploader("작업하던 엑셀 (이어하기)", type=['xlsx'])
    if uploaded_excel:
        if st.button("데이터 병합하기"):
            try:
                loaded_df = pd.read_excel(uploaded_excel)
                if st.session_state.master_df is not None:
                    st.session_state.master_df = pd.concat([st.session_state.master_df, loaded_df]).drop_duplicates(subset=['자료', '구분'], keep='last')
                else:
                    st.session_state.master_df = loaded_df
                st.success("데이터 병합 완료!")
            except Exception as e: st.error(f"엑셀 로드 실패: {e}")

    # 백업 및 복구
    col_b1, col_b2 = st.columns(2)
    with col_b1:
        if st.button("☁️ 클라우드 백업"):
            if save_backup_to_cloud(MODE_KEY, st.session_state.master_df):
                st.toast("백업 성공!", icon="☁️")
    with col_b2:
        if st.button("🔄 복구"):
            restored = load_backup_from_cloud(MODE_KEY)
            if restored is not None:
                st.session_state.master_df = restored
                st.toast("복구 성공!", icon="✅")

# --- 메인: 파일 업로드 및 뷰어 ---
st.subheader("2. 교과서/자료 업로드")
main_file = st.file_uploader("PDF 또는 이미지 파일", type=['pdf', 'png', 'jpg'])

if main_file:
    # 파일 변경 감지
    if st.session_state.file_hash != main_file.id:
        st.session_state.file_hash = main_file.id
        st.session_state.page_idx = 0
        st.session_state.analysis_result = []
    
    # 탭 구성 (Req 3: 페이지 이동)
    tab_view, tab_data, tab_stat = st.tabs(["📝 분석 및 수정", "📊 전체 데이터", "📈 통계"])
    
    with tab_view:
        col_img, col_txt = st.columns([1, 1])
        
        # [좌측] 이미지 뷰어
        with col_img:
            st.info("📄 미리보기")
            img_bytes = get_page_image_bytes(main_file, st.session_state.page_idx)
            if img_bytes:
                st.image(img_bytes, use_container_width=True)
            
            # PDF 네비게이션
            if "pdf" in main_file.type:
                c1, c2 = st.columns([1, 1])
                with c1:
                    if st.button("◀ 이전 페이지"):
                        st.session_state.page_idx = max(0, st.session_state.page_idx - 1)
                        st.rerun()
                with c2:
                    if st.button("다음 페이지 ▶"):
                        st.session_state.page_idx += 1 # 최대 페이지 체크는 생략(fitz가 알아서 처리)
                        st.rerun()
                
                # 쪽수 설정 (패치 6번)
                st.session_state.start_page_offset = st.number_input("시작 쪽수 오프셋", value=st.session_state.start_page_offset)
                current_real_page = st.session_state.page_idx + st.session_state.start_page_offset
                st.caption(f"현재 PDF {st.session_state.page_idx+1}페이지 → 교과서 {current_real_page}쪽으로 저장됨")

        # [우측] 텍스트 및 분석
        with col_txt:
            st.info("📝 텍스트 추출 및 분석")
            # 텍스트 추출 (Req 4: 직접 입력 가능)
            extracted_txt = extract_text_unified(main_file, st.session_state.page_idx)
            input_text = st.text_area("분석 대상 텍스트 (수정 가능)", value=extracted_txt, height=200)
            
            if st.button("🚀 분석 실행 (CoT + Vision)", type="primary"):
                with st.spinner("AI가 생각하며 분석 중..."):
                    # 텍스트가 너무 적으면 이미지 통째로 전송 (Vision Mode)
                    send_img = img_bytes if len(input_text.strip()) < 10 else None
                    
                    results = analyze_hybrid(input_text, send_img, sheet_data, MODE_KEY)
                    
                    # 결과 가공
                    processed = []
                    # 이모지 매핑
                    origin_map = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                    pos_map = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                    
                    # 빈도 계산
                    all_originals = [r['original_word'] for r in results]
                    counts = Counter(all_originals)
                    
                    seen_roots = set()
                    for item in results:
                        root = item['root_word']
                        # 중복된 Root는 하나로 합쳐서 보여주되, 원본 단어들을 병기하면 좋음 (단순화를 위해 Root 기준 유니크)
                        if root not in seen_roots:
                            processed.append({
                                "delete": False,
                                "count": f"{counts[item['original_word']]}회", # 단순화된 빈도 (엄밀히는 Root 기준 합산이 좋음)
                                "original_word": item['original_word'],
                                "root_word": root,
                                "origin": origin_map.get(item['origin'], item['origin']),
                                "pos": pos_map.get(item['pos'], item['pos'])
                            })
                            seen_roots.add(root)
                    
                    st.session_state.analysis_result = processed

            # [분석 결과 에디터] (Req 2, 4)
            if st.session_state.analysis_result:
                df_res = pd.DataFrame(st.session_state.analysis_result)
                edited_df = st.data_editor(
                    df_res,
                    column_config={
                        "delete": st.column_config.CheckboxColumn("삭제"),
                        "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
                        "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사"])
                    },
                    use_container_width=True,
                    num_rows="dynamic",
                    key="editor_main"
                )
                
                # 저장 및 학습 버튼
                col_s1, col_s2 = st.columns(2)
                with col_s1:
                    if st.button("💾 결과 저장 (Merge)"):
                        # [패치 8번] 엑셀 저장 로직 (쪽수1, 쪽수2...)
                        valid_rows = edited_df[edited_df['delete'] == False]
                        if st.session_state.master_df is None:
                            st.session_state.master_df = pd.DataFrame(columns=["구분", "자료", "출연횟수", "쪽수1"])
                        
                        master = st.session_state.master_df
                        page_val = str(current_real_page if "pdf" in main_file.type else st.session_state.manual_page_input)
                        
                        for _, row in valid_rows.iterrows():
                            clean_origin = row['origin'].replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '')
                            root = row['root_word']
                            
                            # 기존 단어 검색
                            mask = (master['자료'] == root) & (master['구분'] == clean_origin)
                            if mask.any():
                                idx = master[mask].index[0]
                                # 빈 쪽수 컬럼 찾기
                                filled = [c for c in master.columns if '쪽수' in c and pd.notna(master.at[idx, c])]
                                next_col = f"쪽수{len(filled)+1}"
                                if next_col not in master.columns: master[next_col] = None
                                master.at[idx, next_col] = page_val
                            else:
                                new_row = {"구분": clean_origin, "자료": root, "출연횟수": 0, "쪽수1": page_val}
                                master = pd.concat([master, pd.DataFrame([new_row])], ignore_index=True)
                        
                        # 출연횟수 재계산
                        count_cols = [c for c in master.columns if '쪽수' in c]
                        master['출연횟수'] = master[count_cols].notna().sum(axis=1)
                        st.session_state.master_df = master
                        
                        # 자동 백업
                        save_backup_to_cloud(MODE_KEY, master)
                        st.toast("저장되었습니다!", icon="💾")

                with col_s2:
                    if st.button("🎓 수정사항 학습"):
                        if sheet:
                            logs = []
                            for _, row in edited_df.iterrows():
                                logs.append([
                                    datetime.now().isoformat(),
                                    row['original_word'], row['root_word'],
                                    row['origin'].replace('🔵 ', '').replace('🟢 ', ''),
                                    row['pos'].replace('📦 ', ''),
                                    'modify', 'Manual'
                                ])
                            send_data_with_retry(sheet, logs, is_multiple=True)
                            st.toast("학습 완료!", icon="🧠")

    with tab_data:
        if st.session_state.master_df is not None:
            st.dataframe(st.session_state.master_df, use_container_width=True)
            
            # 엑셀 다운로드
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                st.session_state.master_df.to_excel(writer, index=False)
            st.download_button("📥 전체 엑셀 다운로드", buffer.getvalue(), "korean_analysis_final.xlsx")

    with tab_stat:
        if st.session_state.master_df is not None:
            df = st.session_state.master_df
            st.metric("총 단어 수", len(df))
            if '구분' in df.columns:
                st.bar_chart(df['구분'].value_counts())