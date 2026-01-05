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

# =========================================================
# ⚙️ 설정
# =========================================================
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk" # (비상용)
except:
    API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk"

MODEL_NAME = "gemini-2.5-flash"
SHEET_NAME = "Korean_DB" 
TRUST_THRESHOLD = 3 

st.set_page_config(page_title="국어활동 AI 분석기", page_icon="📝", layout="wide")

# =========================================================
# 🔐 구글 시트 연결 (이원화 로직 적용)
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

# [핵심 변경] 모드에 따라 다른 탭(Worksheet)을 가져오는 함수
def get_sheet_data_fresh(mode_key):
    client = get_google_sheet_client()
    if not client: return None, []
    
    # 모드에 따른 시트 이름 매핑
    target_sheet_name = "South_Korea" if mode_key == "SOUTH" else "North_Korea"
    
    try:
        # 파일 열기
        spreadsheet = client.open(SHEET_NAME)
        # 탭(워크시트) 선택
        try:
            sheet = spreadsheet.worksheet(target_sheet_name)
        except gspread.WorksheetNotFound:
            st.error(f"❌ '{target_sheet_name}' 시트를 찾을 수 없습니다. 구글 시트 하단 탭 이름을 확인해주세요.")
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
    
    # 학습 데이터 프롬프트 생성 (공통 로직)
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

# [핵심 변경] 모드에 따라 AI 페르소나(역할) 변경
def get_analysis_hybrid(text, sheet_data, mode_key):
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    # 1. 공통 역할
    role_definition = "국어학 전문가로서 문맥을 고려하여 실질 형태소(알맹이 단어)를 분석하세요."
    
    # 2. 모드별 특수 지침 (페르소나 이원화)
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

    # [수정 완료] JSON 예시의 중괄호를 {{ }}로 이중 처리하여 f-string 오류 완벽 해결
    base_instruction = f"""
    {role_definition}
    {mode_instruction}
    
    [핵심 작성 규칙]
    - original_word: 문장에서 **실제로 쓰인 형태 그대로(활용형 포함)** 적으세요. (예: '먹었습니다' -> '먹었습니다')
    - root_word: 사전에 등재된 **기본형(원형)**으로 적으세요.
    
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
    
    # UI 제거됨 (st.status 등의 시각적 요소 없음)
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

# [사이드바] 모드 선택 UI 추가
with st.sidebar:
    st.header("🏳️ 분석 모드 선택")
    mode_selection = st.radio(
        "분석할 언어 환경을 선택하세요",
        ("🇰🇷 대한민국 표준어", "🇰🇵 북한 문화어"),
        index=0
    )
    
    # 모드 키 설정 (SOUTH / NORTH)
    MODE_KEY = "SOUTH" if "대한민국" in mode_selection else "NORTH"
    
    st.info(f"현재 **{mode_selection}** 모드로 동작합니다.\n\n연결된 시트: {('South_Korea' if MODE_KEY=='SOUTH' else 'North_Korea')}")
    st.markdown("---")

# [동적 연결] 선택된 모드에 따라 시트 데이터 로드
sheet, sheet_data = get_sheet_data_fresh(MODE_KEY)

if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None
if 'excel_buffer' not in st.session_state:
    st.session_state.excel_buffer = None
if 'master_df' not in st.session_state:
    st.session_state.master_df = None

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

    if sheet: st.success(f"🌏 두뇌 연결됨 ({len(sheet_data)}건)")
    else: st.error("❌ 두뇌 연결 실패")
    
    st.markdown("---")
    
    # [수동 추가]
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
                    
                    # 현재 모드 결과에 즉시 반영
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
                    
                    st.toast(f"✅ '{add_orig}' 추가 완료! ({MODE_KEY} 모드)", icon="🎓")
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

col1, col2 = st.columns([4, 1])
with col1:
    input_text = st.text_area("분석할 문장을 입력하세요", height=150, placeholder="예: 나는 어제 맛있는 비빔냉면을 먹었다.")
with col2:
    page_num = st.text_input("쪽수", value="1")
    analyze_btn = st.button("🚀 분석 실행", use_container_width=True)

# ---------------------------------------------------------
# 🚀 분석 실행 (모드 키 전달)
# ---------------------------------------------------------
if analyze_btn and input_text:
    with st.spinner(f"{mode_selection} 모드로 분석 중입니다..."):
        # [수정] UI 요소 전달 제거 (pure function call)
        raw_results = get_analysis_hybrid(input_text, sheet_data, MODE_KEY)
        
        if raw_results:
            validation_text = input_text.replace(" ", "")
            POS_WHITELIST = ['명사', '동사', '형용사', '부사', '관형사'] 
            blacklist = get_blacklist_from_sheet(sheet_data)
            problematic_words = get_problematic_words(sheet_data)
            
            # [Step 1] 기본 필터링
            pre_filtered_items = []
            for item in raw_results:
                original = item.get('original_word', '').replace(" ", "")
                root = item.get('root_word', '')
                pos = item.get('pos', '')
                
                if original not in validation_text: pass 
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
            
            # [Step 2] 그룹화
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

            # [Step 3] 최종 리스트 생성
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

# 결과 화면
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
    
    col_del, col_save = st.columns([1, 4])
    
    with col_del:
        if st.button("⛔ 체크한 단어 삭제 및 학습", type="secondary"):
            to_delete = edited_df[edited_df['delete_check'] == True]
            if not to_delete.empty:
                if sheet:
                    try:
                        rows_to_add = []
                        for _, row in to_delete.iterrows():
                            rows_to_add.append([
                                datetime.now().isoformat(),
                                row['original_word'], 
                                row['root_word'],
                                "", "", 'delete', input_text
                            ])
                        sheet.append_rows(rows_to_add)
                        st.toast(f"🗑️ 삭제 학습 완료! ({MODE_KEY} 탭에 저장)", icon="✅")
                        remaining = edited_df[edited_df['delete_check'] == False].to_dict('records')
                        st.session_state.analysis_result = remaining
                        time.sleep(1)
                        st.rerun()
                    except Exception as e:
                        st.error(f"삭제 오류: {e}")
            else:
                st.toast("삭제할 단어를 체크해주세요.", icon="⚠️")

    st.markdown("---")
    
    with col_save:
        # [누적 저장 로직]
        if st.button("💾 엑셀에 저장 (누적 저장)", type="primary"):
            final_data = edited_df[edited_df['delete_check'] == False].to_dict('records')
            
            base_df = st.session_state.master_df
            if base_df is None:
                 base_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])

            for c in base_df.columns:
                if '쪽수' in c: base_df[c] = base_df[c].astype(object)
                
            new_rows = []
            saved_roots = set() 
            learning_logs = []

            for item in final_data:
                item['origin'] = clean_value_for_save(item['origin'])
                item['pos'] = clean_value_for_save(item['pos'])
                
                # 학습 로그
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
                
                try:
                    cnt = int(str(item['count']).replace('회',''))
                except: cnt = 1
                
                val = f"{page_num}_{cnt}" if cnt > 1 else page_num
                origin_val = item.get('origin', '고')
                
                if root in base_df['자료'].values:
                    idx = base_df[base_df['자료'] == root].index[0]
                    filled = base_df.loc[idx].filter(like='쪽수').notna().sum()
                    col = f"쪽수{filled+1}"
                    if col not in base_df.columns: base_df[col] = float('nan')