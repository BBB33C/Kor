import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import Counter
from datetime import datetime
import time

# [안전 장치 1] 라이브러리 부재 시 프로그램 멈춤 방지
try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

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

MODEL_NAME = "gemini-2.5-flash"
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
            st.error(f"❌ '{target_sheet_name}' 시트를 찾을 수 없습니다. 구글 시트 하단 '+' 버튼을 눌러 탭을 만들고 이름을 변경해주세요.")
            return None, []
            
        data = sheet.get_all_records()
        return sheet, data
    except Exception as e:
        st.error(f"구글 시트 연결 오류: {e}")
        return None, []

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
        st.error(f"❌ 서버 통신 오류 (타임아웃 등): {e}")
        return None

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

# [핵심] 1쪽 단위 추출 + 상/하단 크롭핑 (쪽수/머리말 제거)
def extract_text_from_page(pdf_file, page_index):
    if not PDF_AVAILABLE:
        return "PDF 라이브러리가 설치되지 않았습니다."
    
    text_content = ""
    try:
        with pdfplumber.open(pdf_file) as pdf:
            # 인덱스 범위 체크
            if page_index < 0 or page_index >= len(pdf.pages):
                return ""
            
            page = pdf.pages[page_index]
            width = page.width
            height = page.height
            
            # 상단 10%, 하단 10% 제거 (본문만 남김)
            crop_box = (0, height * 0.1, width, height * 0.9)
            
            try:
                cropped_page = page.crop(crop_box)
                extracted = cropped_page.extract_text()
            except:
                extracted = page.extract_text() # 크롭 실패시 원본
            
            if extracted:
                text_content = extracted
                
    except Exception as e:
        return f"PDF 읽기 오류: {str(e)}"
        
    return text_content

def get_analysis_hybrid(text, sheet_data, mode_key):
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    role_definition = "국어학 전문가로서 문맥을 고려하여 실질 형태소(알맹이 단어)를 분석하세요."
    
    if mode_key == "NORTH":
        mode_instruction = """
        [🇰🇵 북한 문화어 분석 모드]
        - 당신은 '북한 문화어(Munhwa-o)' 전문가입니다.
        - **두음법칙을 적용하지 마세요.** (예: '노동'이 아니라 '로동', '여자'가 아니라 '녀자'가 원형입니다.)
        - 북한 특유의 어휘나 표기법이 있다면 이를 존중하여 원형을 추출하세요.
        """
    else:
        mode_instruction = """
        [🇰🇷 대한민국 표준어 분석 모드]
        - 당신은 '대한민국 표준어' 전문가입니다.
        - 국립국어원 표준 맞춤법과 두음법칙을 준수하세요.
        """

    # [핵심] 동음이의어 구분 지침 추가 (태깅 시스템)
    base_instruction = f"""
    {role_definition}
    {mode_instruction}
    
    [핵심 작성 규칙]
    - original_word: 문장에서 **실제로 쓰인 형태 그대로(활용형 포함)** 적으세요. (예: '먹었습니다' -> '먹었습니다')
    - root_word: 사전에 등재된 **기본형(원형)**으로 적으세요.
    
    [동음이의어 구분 규칙 - 매우 중요!]
    - **사람 이름(인명)**인 경우: 원형 뒤에 **(이름)**을 붙이세요. (예: '지혜가 왔다' -> '지혜(이름)')
    - **지명(장소)**인 경우: 원형 뒤에 **(지명)**을 붙이세요. (예: '서울로 갔다' -> '서울(지명)')
    - 그 외 동음이의어가 명확하면 괄호로 뜻을 구분하세요. (예: '배를 탔다' -> '배(교통)', '배를 먹었다' -> '배(과일)')
    
    [분석 3단계 우선순위]
    1. [최우선] 사용자 학습 규칙(위쪽 내용)이 있다면 무조건 따르세요.
    2. [표준 원형] 특별한 학습 규칙이 없다면, 해당 언어 규범(남/북)에 맞는 사전적 '기본형'을 추출하세요. 굳이 '하다'를 떼어내려 하지 마세요.
    3. [명사 통합] '비빔냉면', '학교앞' 같은 복합명사는 굳이 쪼개지 말고 '하나의 단어'로 분석하세요.
    
    [기본 규칙]
    - 조사, 어미, 접사, 문장부호, 감탄사는 제외하세요.
    - 추출 대상 품사: '명사', '동사', '형용사', '부사', '관형사'
    - 어원 분류: '고'(고유어), '한'(한자어), '외'(외래어), '혼'(혼종어)
    
    형식: [{{"original_word": "문장에_나온_그대로", "root_word": "기본형", "origin": "고", "pos": "명사"}}]
    """
    
    chunks = split_text_smartly(text)
    all_results = []
    
    for i, chunk in enumerate(chunks):
        if not chunk.strip(): continue
        
        keywords = preprocess_with_morphology(chunk)
        if keywords:
            prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{chunk}"\n힌트(참고만 할 것): {', '.join(keywords)}"""
        else:
            prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{chunk}" """
            
        chunk_result = api_call_direct(prompt)
        if chunk_result:
            all_results.extend(chunk_result)
            
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
            elif val != 'nan' and val != '':
                total += 1
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
        if '자료' in df.columns and '출연횟수' in df.columns:
            return df
        else:
            return None
    except: return None

# =========================================================
# 🖥️ 메인 화면 로직
# =========================================================
st.title("📝 국어활동 AI 분석기")

with st.sidebar:
    st.header("🏳️ 분석 모드 선택")
    mode_selection = st.radio(
        "분석할 언어 환경을 선택하세요",
        ("🇰🇷 대한민국 표준어", "🇰🇵 북한 문화어"),
        index=0
    )
    
    MODE_KEY = "SOUTH" if "대한민국" in mode_selection else "NORTH"
    
    if 'last_mode' not in st.session_state:
        st.session_state.last_mode = MODE_KEY
    
    if st.session_state.last_mode != MODE_KEY:
        st.session_state.analysis_result = None
        st.session_state.last_mode = MODE_KEY
        st.rerun()

    connected_tab_name = 'South_Korea' if MODE_KEY=='SOUTH' else 'North_Korea'
    st.success(f"현재 **[{mode_selection}]** 모드입니다.\n\n학습 데이터가 **'{connected_tab_name}'** 탭에 저장됩니다.")
    st.markdown("---")

sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)

if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None
if 'excel_buffer' not in st.session_state:
    st.session_state.excel_buffer = None
if 'master_df' not in st.session_state:
    st.session_state.master_df = None

# PDF 관련 상태 관리
if 'pdf_file' not in st.session_state: st.session_state.pdf_file = None
if 'total_pages' not in st.session_state: st.session_state.total_pages = 0
if 'current_page_idx' not in st.session_state: st.session_state.current_page_idx = 0 # 0부터 시작
if 'user_page_num' not in st.session_state: st.session_state.user_page_num = "1" # 사용자가 입력하는 쪽수

uploaded_df = None

with st.sidebar:
    st.header("📂 이어하기")
    uploaded_excel = st.file_uploader("작업하던 엑셀 파일", type=['xlsx'])
    
    if uploaded_excel:
        uploaded_df = load_excel_safely(uploaded_excel)
        if uploaded_df is not None:
             st.success(f"📂 파일 로드됨: {len(uploaded_df)}개 단어")
             if st.session_state.master_df is None:
                 st.session_state.master_df = uploaded_df.copy()
        else:
             st.caption("ℹ️ 빈 파일 혹은 양식이 다른 파일입니다.")
    else:
        if st.session_state.master_df is None:
            st.session_state.master_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])

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
                    sheet.append_row(row)
                    
                    if st.session_state.analysis_result is not None:
                        formatted_pos = {
                            '명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사',
                            '부사': '⚡ 부사', '관형사': '🔍 관형사'
                        }.get(add_pos, add_pos)
                        
                        new_item = {
                            'original_word': add_orig, 'root_word': add_root,
                            'origin': add_emoji_to_origin(add_origin), 'pos': formatted_pos,
                            'status': '✅ 수동', 'count': '1회', 'delete_check': False
                        }
                        st.session_state.analysis_result.append(new_item)
                    
                    st.toast(f"✅ '{add_orig}' 추가 완료! ({MODE_KEY} 학습데이터에 저장)", icon="🎓")
                    time.sleep(1)
                    st.rerun()

    st.markdown("---")
    st.subheader("🔍 이력 검색")
    search_query = st.text_input("궁금한 단어")
    if search_query and sheet_data:
        history = [row for row in sheet_data if search_query in str(row.get('root_word')) or search_query in str(row.get('original_word'))]
        if history:
            for h in history[-3:]:
                st.caption(f"{h['timestamp'][:10]} [{h['action']}] {h['original_word']} -> {h['root_word']}")
        else: st.caption("이력이 없습니다.")

# [UI] 메인 화면 구성
col1, col2 = st.columns([3, 1])

extracted_text = ""
    
with col1:
    st.subheader("📄 텍스트 입력 및 PDF 뷰어")
    
    if PDF_AVAILABLE:
        # 파일이 바뀔 때마다 초기화
        uploaded_pdf = st.file_uploader("교과서 PDF 파일 (1쪽 단위 자동 분석)", type=['pdf'], key='pdf_uploader')
        if uploaded_pdf and uploaded_pdf != st.session_state.pdf_file:
            st.session_state.pdf_file = uploaded_pdf
            st.session_state.current_page_idx = 0
            st.session_state.user_page_num = "1"
            st.session_state.analysis_result = None
            try:
                with pdfplumber.open(uploaded_pdf) as pdf:
                    st.session_state.total_pages = len(pdf.pages)
            except: pass
            st.rerun()
            
        if st.session_state.pdf_file and st.session_state.total_pages > 0:
            # 네비게이션 UI
            c_prev, c_info, c_next = st.columns([1, 2, 1])
            with c_prev:
                if st.button("◀ 이전 장"):
                    if st.session_state.current_page_idx > 0:
                        st.session_state.current_page_idx -= 1
                        st.session_state.analysis_result = None # 페이지 이동 시 결과 초기화
                        try:
                            # 쪽수 추정 (현재 인덱스 + 1)
                            st.session_state.user_page_num = str(st.session_state.current_page_idx + 1)
                        except: pass
                        st.rerun()
            with c_next:
                if st.button("다음 장 ▶"):
                    if st.session_state.current_page_idx < st.session_state.total_pages - 1:
                        st.session_state.current_page_idx += 1
                        st.session_state.analysis_result = None
                        try:
                            st.session_state.user_page_num = str(st.session_state.current_page_idx + 1)
                        except: pass
                        st.rerun()
            with c_info:
                st.markdown(f"<div style='text-align:center; padding-top:10px;'><b>PDF {st.session_state.current_page_idx + 1} / {st.session_state.total_pages} 번째 장</b></div>", unsafe_allow_html=True)
            
            # 텍스트 자동 추출
            extracted_text = extract_text_from_page(st.session_state.pdf_file, st.session_state.current_page_idx)
            if "오류" in extracted_text: st.error(extracted_text)
            
    else:
        st.warning("⚠️ PDF 자동 추출 기능을 사용하려면 'pdfplumber' 라이브러리 설치가 필요합니다.")

    # 텍스트 에디터 (자동 채움)
    input_text = st.text_area(
        "분석할 텍스트 (직접 수정 가능)", 
        value=extracted_text if extracted_text else "",
        height=300, 
        placeholder="직접 입력하거나 PDF를 업로드하면 내용이 나타납니다."
    )

with col2:
    st.write("") 
    st.write("") 
    st.write("") 
    st.write("") 
    
    st.markdown("##### ⚙️ 저장 설정")
    # 사용자가 직접 수정 가능한 실제 쪽수
    user_page_val = st.text_input("저장될 실제 쪽수", value=st.session_state.user_page_num, key="page_input")
    # 입력값이 바뀌면 세션에 업데이트
    if user_page_val != st.session_state.user_page_num:
        st.session_state.user_page_num = user_page_val
    
    analyze_btn = st.button("🚀 분석 실행", use_container_width=True, type="primary")

# 분석 로직
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
                
                # 태깅된 단어 (예: 지혜(이름))는 원본 검증 시 태그 떼고 검사
                orig_check = original.split('(')[0]
                if orig_check not in validation_text: pass 
                
                if not pos or pos not in POS_WHITELIST: continue
                if original in blacklist or root in blacklist: continue
                
                if item.get('origin') == '순': item['origin'] = '고'
                item['origin'] = add_emoji_to_origin(item.get('origin', ''))
                
                pos_map = {
                    '명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사',
                    '부사': '⚡ 부사', '관형사': '🔍 관형사'
                }
                item['pos'] = pos_map.get(pos, pos)
                
                pre_filtered_items.append(item)
            
            grouped_data = {} 
            for item in pre_filtered_items:
                root = item['root_word']
                if root not in grouped_data:
                    grouped_data[root] = {
                        'root_word': root,
                        'origin': item['origin'],
                        'pos': item['pos'],
                        'originals': []
                    }
                grouped_data[root]['originals'].append(item['original_word'])

            final_results = []
            for root, info in grouped_data.items():
                orig_counts = Counter(info['originals'])
                formatted_original = ", ".join([f"{word}({cnt})" for word, cnt in orig_counts.items()])
                
                total_cnt = sum(orig_counts.values())
                
                is_trusted = check_trust_level_strict(root, st.session_state.master_df, problematic_words)
                status = '✅ 자동' if is_trusted else '📝 검토'
                
                final_results.append({
                    'delete_check': False,
                    'status': status,
                    'count': f"{total_cnt}회",
                    'original_word': formatted_original,
                    'root_word': root,
                    'origin': info['origin'],
                    'pos': info['pos']
                })
                
            st.session_state.analysis_result = final_results
        else: pass

# 결과 및 투 트랙 저장 버튼
if st.session_state.analysis_result:
    st.markdown("### 📊 분석 결과 (수정 및 삭제)")
    
    df_display = pd.DataFrame(st.session_state.analysis_result)
    
    column_config = {
        "delete_check": st.column_config.CheckboxColumn("삭제", width="small"),
        "status": st.column_config.TextColumn("상태", width="medium", disabled=True),
        "count": st.column_config.TextColumn("빈도(합계)", width="small", disabled=True),
        "original_word": st.column_config.TextColumn("원본 단어(빈도)", disabled=True, width="large"),
        "root_word": "원형",
        "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
        "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사"])
    }
    
    cols = ["delete_check", "status", "count", "original_word", "root_word", "origin", "pos"]
    
    edited_df = st.data_editor(
        df_display[cols] if not df_display.empty else df_display,
        column_config=column_config, 
        use_container_width=True, 
        num_rows="fixed",
        key="editor"
    )
    
    # 하단 버튼 영역 (삭제 / 투 트랙 저장)
    btn_col1, btn_col2, btn_col3 = st.columns([1, 2, 2])
    
    with btn_col1:
        if st.button("⛔ 체크 삭제", type="secondary"):
            to_delete = edited_df[edited_df['delete_check'] == True]
            if not to_delete.empty:
                if sheet:
                    try:
                        rows_to_add = []
                        for _, row in to_delete.iterrows():
                            rows_to_add.append([
                                datetime.now().isoformat(),
                                row['original_word'], row['root_word'], "", "", 'delete', input_text
                            ])
                        sheet.append_rows(rows_to_add)
                        st.toast(f"🗑️ 삭제 학습 완료!", icon="✅")
                        remaining = edited_df[edited_df['delete_check'] == False].to_dict('records')
                        st.session_state.analysis_result = remaining
                        time.sleep(1)
                        st.rerun()
                    except: st.error("삭제 오류")

    # 공통 저장 함수
    def save_logic(df_to_save, page_str):
        final_data = df_to_save[df_to_save['delete_check'] == False].to_dict('records')
        base_df = st.session_state.master_df
        if base_df is None:
             base_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])
        for c in base_df.columns:
            if '쪽수' in c: base_df[c] = base_df[c].astype(object)
        
        new_rows = []
        learning_logs = []
        saved_roots = set()

        for item in final_data:
            item['origin'] = clean_value_for_save(item['origin'])
            item['pos'] = clean_value_for_save(item['pos'])
            
            learning_logs.append({
                'timestamp': datetime.now().isoformat(),
                'original_word': item['original_word'], 
                'root_word': item['root_word'],
                'origin': item['origin'],
                'pos': item['pos'],
                'action': 'modify',
                'context': input_text
            })
            
            root = item['root_word']
            if root in saved_roots: continue
            saved_roots.add(root)
            
            try: cnt = int(str(item['count']).replace('회',''))
            except: cnt = 1
            
            val = f"{page_str}_{cnt}" if cnt > 1 else page_str
            origin_val = item.get('origin', '고')
            
            if root in base_df['자료'].values:
                idx = base_df[base_df['자료'] == root].index[0]
                filled = base_df.loc[idx].filter(like='쪽수').notna().sum()
                col = f"쪽수{filled+1}"
                if col not in base_df.columns: base_df[col] = float('nan')
                base_df.at[idx, col] = val
            else:
                new_rows.append({'구분': origin_val, '자료': root, '쪽수1': val})
        
        if new_rows:
            base_df = pd.concat([base_df, pd.DataFrame(new_rows)], ignore_index=True)
        
        base_df['출연횟수'] = base_df.apply(calculate_total_appearances, axis=1)
        base_df['sort'] = base_df['구분'].map({'고':1, '순':1, '한':2, '외':3, '혼':4}).fillna(5)
        base_df = base_df.sort_values(['sort', '자료']).drop('sort', axis=1)
        st.session_state.master_df = base_df
        
        output_excel = io.BytesIO()
        with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
            base_df.to_excel(writer, index=False)
        output_excel.seek(0)
        st.session_state.excel_buffer = output_excel

        if sheet and learning_logs:
            try:
                rows_to_add = [list(log.values()) for log in learning_logs]
                sheet.append_rows(rows_to_add)
            except: pass
        return True

    with btn_col2:
        # 투 트랙 1: 저장하고 계속하기 (고속 모드)
        if st.button("💾 저장하고 다음 쪽(▶) 이동", type="primary", use_container_width=True):
            if save_logic(edited_df, st.session_state.user_page_num):
                # 페이지 넘김 로직
                if st.session_state.current_page_idx < st.session_state.total_pages - 1:
                    st.session_state.current_page_idx += 1
                    # 쪽수도 +1
                    try:
                        curr_p = int(st.session_state.user_page_num)
                        st.session_state.user_page_num = str(curr_p + 1)
                    except: pass
                    st.session_state.analysis_result = None # 결과 초기화
                    st.toast("✅ 저장 완료! 다음 장으로 이동했습니다.", icon="🏃")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.success("마지막 페이지입니다! 다운로드를 이용하세요.")

    with btn_col3:
        # 투 트랙 2: 저장만 하기 (종료/다운로드 모드)
        if st.button("💾 저장만 하기 (종료)", use_container_width=True):
            if save_logic(edited_df, st.session_state.user_page_num):
                st.success("✅ 저장되었습니다. 아래 버튼으로 엑셀을 받으세요.")

    # 다운로드 버튼 (저장 후 버퍼가 있을 때만 표시)
    if st.session_state.excel_buffer:
        st.download_button(
            label="📥 엑셀파일 다운로드",
            data=st.session_state.excel_buffer,
            file_name="국어활동_분석결과_통합.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="secondary",
            use_container_width=True
        )