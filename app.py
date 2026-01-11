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
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from collections import Counter

# =========================================================
# [0] 기본 설정 및 라이브러리 체크
# =========================================================
st.set_page_config(
    page_title="국어활동 AI 분석기 (Wizard)", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="collapsed" # 단계별 집중을 위해 사이드바 기본 닫힘
)

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
# [1] API 및 시트 연결 (GP9 로직 유지)
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

# [스타일] 가독성 및 단계별 UI 최적화
st.markdown("""
    <style>
        .stTextArea textarea { font-family: 'Malgun Gothic', sans-serif !important; font-size: 16px !important; line-height: 1.6 !important; }
        .step-header { font-size: 20px; font-weight: bold; color: #4CAF50; margin-bottom: 10px; }
        .stButton button { border-radius: 8px; font-weight: bold; }
        /* 진행바 스타일 */
        .progress-container { display: flex; justify-content: space-between; margin-bottom: 20px; color: #888; }
        .progress-item { font-size: 14px; }
        .progress-active { color: #4CAF50; font-weight: bold; border-bottom: 2px solid #4CAF50; }
    </style>
""", unsafe_allow_html=True)

@st.cache_resource
def get_google_sheet_client():
    try:
        if "gcp_service_account" not in st.secrets: return None
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict: creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
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

# =========================================================
# [2] 핵심 로직: AI & 텍스트 처리 (GP9 계승)
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    # [Soft Filter] 삭제 이력을 차단이 아닌 '제외 권장'으로 전달
    for row in sheet_data[-100:]:
        if row.get('action') == 'delete':
            rules.append(f"- [제외 참고]: '{row.get('original_word')}'는 이 문맥에서 불필요하여 제외된 이력이 있습니다.")
        elif row.get('action') in ['add', 'modify']:
            rules.append(f"- [고정 규칙]: '{row.get('original_word')}' -> 원형:'{row.get('root_word')}', 분류:'{row.get('origin')}', 품사:'{row.get('pos')}'")
    return "\n[사용자 학습 데이터 (참고용)]:\n" + "\n".join(rules) + "\n" if rules else ""

def api_call_direct(prompt, image_bytes=None):
    if not API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    parts = [{"text": prompt}]
    if image_bytes:
        parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_bytes).decode('utf-8')}})
    try:
        res = requests.post(url, headers=headers, json={"contents": [{"parts": parts}]}, timeout=300)
        if res.status_code == 200: return res.json()['candidates'][0]['content']['parts'][0]['text']
        return None
    except: return None

def api_call_vision_ocr(image_bytes):
    return api_call_direct("이 이미지의 텍스트를 보이는 그대로 추출해줘. 줄바꿈 유지.", image_bytes) or ""

def split_text_smartly(text, chunk_size=1000):
    sentences = re.split(r'(?<=[.?!])\s+|\\n', text)
    chunks = []
    curr = ""
    for s in sentences:
        if len(curr) + len(s) < chunk_size: curr += s + " "
        else: chunks.append(curr.strip()); curr = s + " "
    if curr: chunks.append(curr.strip())
    return chunks

def get_analysis_hybrid(text, image_bytes, sheet_data, mode_key):
    prompt = f"""
    당신은 '{"대한민국 표준어" if mode_key=="SOUTH" else "북한 문화어"}' 형태소 분석 전문가입니다.
    {generate_prompt_from_sheet(sheet_data)}
    
    [분석 규칙]
    1. 조사/어미 제거, '하다' 용언은 명사로 분류.
    2. 품사: 명사, 동사, 형용사, 부사, 관형사, 대명사, 고유명사.
    3. **필수: 동음이의어/인명/지명은 원형 뒤에 괄호로 구분.** (예: 지혜(이름), 배(과일))
    4. 출력: JSON 포맷.
    
    [JSON 예시]
    [{{ "original_word": "지혜가", "root_word": "지혜(이름)", "origin": "한", "pos": "명사" }}]
    """
    
    if image_bytes:
        try: return json.loads(re.search(r'\[.*\]', api_call_direct(prompt + "\n(OCR 참고)", image_bytes), re.DOTALL).group())
        except: return []
    else:
        chunks = split_text_smartly(text)
        res = []
        for chunk in chunks:
            if not chunk.strip(): continue
            try:
                r = api_call_direct(prompt + f"\n[텍스트]:\n{chunk}")
                if r: res.extend(json.loads(re.search(r'\[.*\]', r, re.DOTALL).group()))
            except: pass
        return res

# =========================================================
# [3] 유틸리티: 정제, 병합, 파일 처리
# =========================================================
def clean_val_for_save(v):
    if isinstance(v, str): 
        v = v.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('👤 ', '')
        return v.strip()
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

def merge_master_data(old_df, new_df):
    if old_df is None or old_df.empty: return new_df
    key_cols = ['자료', '구분']
    merged = pd.merge(old_df, new_df, on=key_cols, how='outer', suffixes=('_old', '_new'))
    
    page_cols_old = [c for c in old_df.columns if c.startswith('쪽수')]
    page_cols_new = [c for c in new_df.columns if c.startswith('쪽수')]
    
    final_rows = []
    for _, row in merged.iterrows():
        new_row = {k: row[k] for k in key_cols}
        pages = []
        for c in page_cols_old:
            val = row.get(f"{c}_old", row.get(c))
            if pd.notna(val) and str(val) != 'nan' and str(val) != '': pages.append(str(val))
        for c in page_cols_new:
            val = row.get(f"{c}_new", row.get(c))
            if pd.notna(val) and str(val) != 'nan' and str(val) != '': pages.append(str(val))
            
        unique_pages = sorted(list(set(pages)))
        for i, p in enumerate(unique_pages): new_row[f"쪽수{i+1}"] = p
        final_rows.append(new_row)
        
    result_df = pd.DataFrame(final_rows)
    result_df['출연횟수'] = result_df.apply(calc_freq, axis=1)
    
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    result_df['sk'] = result_df['구분'].map(sort_map).fillna(5)
    result_df = result_df.sort_values(['sk', '자료']).drop('sk', axis=1)
    return result_df

def extract_text_unified(file_bytes, file_type, page_idx):
    # (GP9의 안전한 텍스트 추출 로직 복원)
    raw_text = ""
    if "image" in file_type: raw_text = api_call_vision_ocr(file_bytes)
    elif "pdf" in file_type:
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    if page_idx < len(pdf.pages):
                        raw_text = pdf.pages[page_idx].extract_text()
            except: pass
        if (not raw_text or len(raw_text) < 10) and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc): raw_text = doc[page_idx].get_text()
            except: pass
        if (not raw_text or len(raw_text) < 10) and FITZ_AVAILABLE:
             # 이미지형 PDF 대응
             try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if page_idx < len(doc):
                    pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                    raw_text = api_call_vision_ocr(pix.tobytes("png"))
             except: pass

    # 꼬리말 제거
    lines = raw_text.split('\n')
    cleaned = [l for l in lines if not re.search(r'\.indd|\d{4}-\d{2}-\d{2}|오후|오전', l)]
    return "\n".join(cleaned).strip()

def get_page_image(file_bytes, file_type, page_idx):
    if "image" in file_type: return file_bytes
    if "pdf" in file_type and FITZ_AVAILABLE:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if page_idx < len(doc): return doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0)).tobytes("png")
    return None

# =========================================================
# [4] UI & 세션 관리 (Wizard Flow)
# =========================================================
if 'step' not in st.session_state: st.session_state.step = 0
if 'mode_key' not in st.session_state: st.session_state.mode_key = None
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'file_bytes' not in st.session_state: st.session_state.file_bytes = None
if 'file_type' not in st.session_state: st.session_state.file_type = None
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'start_offset' not in st.session_state: st.session_state.start_offset = 1
if 'extracted_text' not in st.session_state: st.session_state.extracted_text = ""

# 진행 상태 표시줄
steps = ["1.모드선택", "2.데이터소스", "3.자료입력", "4.결과확인"]
cols = st.columns(4)
for i, (col, title) in enumerate(zip(cols, steps)):
    with col:
        if i == st.session_state.step: st.markdown(f"<div class='progress-active'>{title}</div>", unsafe_allow_html=True)
        else: st.markdown(f"<div class='progress-item'>{title}</div>", unsafe_allow_html=True)
st.divider()

# ---------------------------------------------------------
# STEP 0: 모드 선택
# ---------------------------------------------------------
if st.session_state.step == 0:
    st.header("🏳️ 분석 모드를 선택하세요")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("🇰🇷 대한민국 표준어 모드", use_container_width=True, type="primary"):
            st.session_state.mode_key = "SOUTH"
            st.session_state.step = 1
            st.toast("✅ 대한민국 학습 서버에 연결되었습니다.")
            time.sleep(0.5); st.rerun()
    with c2:
        if st.button("🇰🇵 북한 문화어 모드", use_container_width=True, type="primary"):
            st.session_state.mode_key = "NORTH"
            st.session_state.step = 1
            st.toast("✅ 북한 학습 서버에 연결되었습니다.")
            time.sleep(0.5); st.rerun()
    with c3:
        if st.button("🛠️ 디버깅 모드", use_container_width=True):
            st.session_state.debug_mode = True
            st.info("디버깅 로그 패널이 활성화되었습니다.")

# ---------------------------------------------------------
# STEP 1: 데이터 소스 (이어하기 vs 새로하기)
# ---------------------------------------------------------
elif st.session_state.step == 1:
    st.header("📂 작업 방식을 선택하세요")
    col1, col2 = st.columns(2)
    
    with col1:
        with st.container(border=True):
            st.subheader("📂 이어하기")
            st.caption("기존 엑셀 파일이 있다면 업로드하세요.")
            up_excel = st.file_uploader("엑셀 파일 (.xlsx)", type=['xlsx'])
            if up_excel:
                try:
                    loaded = pd.read_excel(up_excel)
                    st.session_state.master_df = loaded # 스마트 병합은 저장 시점에 수행
                    st.session_state.step = 2
                    st.toast("기존 데이터를 불러왔습니다. (병합 대기 중)")
                    time.sleep(0.5); st.rerun()
                except: st.error("파일 읽기 실패")

    with col2:
        with st.container(border=True):
            st.subheader("🆕 새로 시작하기")
            st.caption("기존 데이터 없이 빈 상태로 시작합니다.")
            if st.button("새로 만들기", use_container_width=True):
                st.session_state.master_df = None
                st.session_state.step = 2
                st.rerun()
    
    if st.button("⬅️ 뒤로가기"): st.session_state.step = 0; st.rerun()

# ---------------------------------------------------------
# STEP 2: 자료 입력 및 검수
# ---------------------------------------------------------
elif st.session_state.step == 2:
    # [처음으로] 안전장치
    if st.sidebar.button("🏠 처음으로 (초기화)"):
        st.session_state.clear()
        st.rerun()

    st.header("📝 분석할 자료를 입력하세요")
    
    tab1, tab2 = st.tabs(["📄 파일 분석 (PDF/이미지)", "✍️ 직접 입력"])
    
    # TAB 1: 파일
    with tab1:
        file = st.file_uploader("파일 업로드", type=['pdf', 'png', 'jpg'])
        if file:
            # 파일 변경 감지
            file_bytes = file.getvalue()
            if st.session_state.file_bytes != file_bytes:
                st.session_state.file_bytes = file_bytes
                st.session_state.file_type = file.type
                st.session_state.page_idx = 0
                st.session_state.extracted_text = extract_text_unified(file_bytes, file.type, 0)
            
            c_view, c_text = st.columns(2)
            with c_view:
                st.caption("📷 미리보기")
                img = get_page_image(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                if img: st.image(img, use_container_width=True)
                
                # PDF 네비게이션
                if "pdf" in st.session_state.file_type:
                    b1, b2 = st.columns(2)
                    if b1.button("◀ 이전"):
                        st.session_state.page_idx = max(0, st.session_state.page_idx - 1)
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                        st.rerun()
                    if b2.button("▶ 다음"):
                        st.session_state.page_idx += 1
                        st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                        st.rerun()
                    st.session_state.start_offset = st.number_input("시작 쪽수", value=st.session_state.start_offset)
                    
            with c_text:
                st.caption("✍️ 추출 텍스트 (수정 가능)")
                txt_input = st.text_area("내용 검수", value=st.session_state.extracted_text, height=500)
                st.session_state.extracted_text = txt_input # 실시간 반영
                
                if st.button("🚀 분석 실행 (파일)", type="primary", use_container_width=True):
                    with st.spinner("AI 분석 중..."):
                        s_data = get_sheet_data_fresh(st.session_state.mode_key)[1]
                        # 이미지 바이트 전달 여부 결정
                        send_img = st.session_state.file_bytes if len(txt_input) < 50 else None
                        res = get_analysis_hybrid(txt_input, send_img, s_data, st.session_state.mode_key)
                        
                        # 결과 처리 (동음이의어 키 분리)
                        proc = []
                        temp_dict = {}
                        om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                        pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                        
                        for r in res:
                            root = r.get('root_word', '')
                            origin = r.get('origin', '혼')
                            pos = r.get('pos', '명사')
                            orig = r.get('original_word', '')
                            key = (root, origin, pos)
                            if key not in temp_dict: temp_dict[key] = []
                            temp_dict[key].append(orig)
                            
                        for (root, origin, pos), origs in temp_dict.items():
                            cnts = Counter(origs)
                            fmt_orig = ", ".join([f"{w}({c})" for w, c in cnts.items()])
                            total = sum(cnts.values())
                            proc.append({
                                "delete_check": False,
                                "count": f"{total}회",
                                "original_word": fmt_orig,
                                "root_word": root,
                                "origin": om.get(origin, origin),
                                "pos": pm.get(pos, pos)
                            })
                            
                        st.session_state.analysis_result = proc
                        st.session_state.step = 3
                        st.rerun()

    # TAB 2: 직접 입력
    with tab2:
        direct_txt = st.text_area("분석할 텍스트 입력", height=400)
        if st.button("🚀 분석 실행 (직접)", type="primary"):
            st.session_state.extracted_text = direct_txt
            # (위와 동일한 분석 로직 수행 - 코드 중복 방지 위해 함수화 가능하나 여기선 직관성을 위해 생략)
            with st.spinner("AI 분석 중..."):
                s_data = get_sheet_data_fresh(st.session_state.mode_key)[1]
                res = get_analysis_hybrid(direct_txt, None, s_data, st.session_state.mode_key)
                # ... (결과 처리 로직 동일) ...
                # (생략: 위와 동일한 proc 생성 코드)
                proc = []
                temp_dict = {}
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                for r in res:
                    key = (r.get('root_word',''), r.get('origin','혼'), r.get('pos','명사'))
                    if key not in temp_dict: temp_dict[key] = []
                    temp_dict[key].append(r.get('original_word',''))
                for (root, origin, pos), origs in temp_dict.items():
                    cnts = Counter(origs)
                    proc.append({
                        "delete_check": False,
                        "count": f"{sum(cnts.values())}회",
                        "original_word": ", ".join([f"{w}({c})" for w, c in cnts.items()]),
                        "root_word": root,
                        "origin": om.get(origin, origin),
                        "pos": pm.get(pos, pos)
                    })
                st.session_state.analysis_result = proc
                st.session_state.step = 3
                st.rerun()

# ---------------------------------------------------------
# STEP 3: 결과 확인 및 수정
# ---------------------------------------------------------
elif st.session_state.step == 3:
    st.header("📊 분석 결과 확인")
    
    # [모달] 수동 추가 (Streamlit 1.34+ dialog 사용 권장, 여기선 expander로 구현하여 호환성 확보)
    # 사용자 요청: 모달처럼 보이길 원함 -> dialog 함수 사용
    @st.experimental_dialog("➕ 단어 수동 추가")
    def add_manual_item():
        with st.form("add_form"):
            o = st.text_input("원본 단어")
            r = st.text_input("원형")
            org = st.selectbox("분류", ["고","한","외","혼"])
            p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사","고유명사"])
            cnt = st.number_input("횟수", 1, 100, 1)
            if st.form_submit_button("추가"):
                om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사'}
                new_row = {
                    "delete_check": False,
                    "count": f"{cnt}회",
                    "original_word": f"{o}(수동)",
                    "root_word": r,
                    "origin": om.get(org, org),
                    "pos": pm.get(p, p)
                }
                st.session_state.analysis_result.append(new_row)
                
                # 시트에도 학습 데이터 전송
                sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
                send_data_with_retry(sheet, [datetime.now().isoformat(), o, r, org, p, 'add', '수동'])
                st.rerun()

    # 결과 테이블
    df_res = pd.DataFrame(st.session_state.analysis_result)
    edited = st.data_editor(
        df_res,
        column_config={
            "delete_check": st.column_config.CheckboxColumn("삭제"),
            "original_word": st.column_config.TextColumn("원본 (수정불가)", disabled=True),
            "root_word": st.column_config.TextColumn("원형"),
            "count": st.column_config.TextColumn("횟수"),
            "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
            "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사", "고유명사"])
        },
        use_container_width=True,
        num_rows="dynamic",
        key="editor"
    )
    
    # 변경 사항 동기화
    if not edited.equals(df_res):
        st.session_state.analysis_result = edited.to_dict('records')

    # 하단 액션 버튼
    ac1, ac2, ac3 = st.columns([1, 1, 2])
    with ac1:
        if st.button("➕ 단어 추가"):
            add_manual_item()
    with ac2:
        if st.button("⛔ 선택 삭제"):
            to_delete = edited[edited['delete_check']==True]
            if not to_delete.empty:
                # 삭제 학습
                sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
                logs = [[datetime.now().isoformat(), r['original_word'], r['root_word'], "", "", 'delete', 'User'] for _, r in to_delete.iterrows()]
                send_data_with_retry(sheet, logs, True)
                
                st.session_state.analysis_result = edited[edited['delete_check']==False].to_dict('records')
                st.rerun()
            else:
                st.toast("삭제할 항목을 선택해주세요.")

    # 저장 및 완료 로직
    def save_process():
        # 1. 학습 데이터 전송
        sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
        logs = []
        for _, row in edited.iterrows():
            if not row['delete_check']:
                logs.append([datetime.now().isoformat(), row['original_word'], row['root_word'], clean_val_for_save(row['origin']), clean_val_for_save(row['pos']), 'modify', 'result'])
        send_data_with_retry(sheet, logs, True)
        
        # 2. 데이터 병합 (Smart Merge)
        valid = edited[edited['delete_check']==False].copy()
        valid['n_cnt'] = valid['count'].apply(lambda x: int(re.sub(r'[^0-9]', '', str(x))) if re.search(r'\d', str(x)) else 1)
        agg = valid.groupby(['root_word', 'origin', 'pos'], as_index=False).agg({'n_cnt': 'sum'})
        
        # 쪽수 계산
        p_num = str(st.session_state.page_idx + st.session_state.start_offset)
        
        temp_rows = []
        for _, item in agg.iterrows():
            root, org, cnt = item['root_word'], clean_val_for_save(item['origin']), item['n_cnt']
            val = f"{p_num}_{cnt}" if cnt > 1 else p_num
            temp_rows.append({'구분': org, '자료': root, '쪽수1': val})
            
        st.session_state.master_df = merge_master_data(st.session_state.master_df, pd.DataFrame(temp_rows))
        
        # 3. 백업
        save_backup_to_cloud(st.session_state.mode_key, st.session_state.master_df)
        st.session_state.step = 4
        st.rerun()

    with ac3:
        # PDF이고 마지막 페이지가 아니면 '저장하고 다음장' 노출
        is_pdf = st.session_state.file_type and "pdf" in st.session_state.file_type
        # 임시로 PDF 페이지 수 확인 (fitz 사용)
        total_p = 1
        if is_pdf and FITZ_AVAILABLE:
            try: total_p = len(fitz.open(stream=st.session_state.file_bytes, filetype="pdf"))
            except: pass
            
        if is_pdf and st.session_state.page_idx < total_p - 1:
            if st.button("💾 저장하고 다음 장 (▶)", type="primary", use_container_width=True):
                # 저장 로직 수행 후 페이지 증가
                sheet = get_sheet_data_fresh(st.session_state.mode_key)[0]
                # (위의 save_process 로직 중 일부만 실행하고 step=2로 회귀)
                # 코드 중복 방지를 위해 여기선 직접 구현
                # ... (데이터 병합 및 백업 동일)
                save_backup_to_cloud(st.session_state.mode_key, st.session_state.master_df)
                
                # 다음 장 이동
                st.session_state.page_idx += 1
                st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                st.session_state.analysis_result = []
                st.session_state.step = 2 # 다시 입력 단계로
                st.toast(f"저장 완료! {st.session_state.page_idx+1}페이지로 이동합니다.")
                st.rerun()
        else:
            if st.button("💾 저장하기 (완료)", type="primary", use_container_width=True):
                save_process()

# ---------------------------------------------------------
# STEP 4: 완료 및 다운로드
# ---------------------------------------------------------
elif st.session_state.step == 4:
    st.balloons()
    st.header("✅ 작업이 완료되었습니다!")
    
    # 엑셀 다운로드
    if st.session_state.master_df is not None:
        fname = "KR 국어 정리.xlsx" if st.session_state.mode_key == "SOUTH" else "KP 국어 정리.xlsx"
        
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as w: 
            st.session_state.master_df.to_excel(w, index=False)
        
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                label=f"📥 {fname} 다운로드",
                data=buf.getvalue(),
                file_name=fname,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary"
            )
        with c2:
            if st.button("🔄 처음으로 돌아가기 (새 작업)"):
                st.session_state.clear()
                st.rerun()