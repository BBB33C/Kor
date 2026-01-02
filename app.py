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

# =========================================================
# ⚙️ 설정
# =========================================================
API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk"
MODEL_NAME = "gemini-2.5-flash"
SHEET_NAME = "Korean_DB"
TRUST_THRESHOLD = 3 

st.set_page_config(page_title="국어활동 AI 분석기", page_icon="📝", layout="wide")

# =========================================================
# 🔐 구글 시트 연결 (캐시 적용)
# =========================================================
@st.cache_resource
def get_google_sheet_client():
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
             creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        return client
    except: return None

def get_sheet_data_cached():
    client = get_google_sheet_client()
    if not client: return None, []
    try:
        sheet = client.open(SHEET_NAME).sheet1
        data = sheet.get_all_records()
        return sheet, data
    except: return None, []

# 이사(Migration) 로직
def sync_json_to_sheet_if_empty(sheet, current_data):
    if not sheet: return
    if len(current_data) > 0: return 

    json_path = "korean_analysis_learning.json"
    if os.path.exists(json_path):
        try:
            with st.spinner("📦 기존 지능을 구글 시트로 이사 중..."):
                with open(json_path, 'r', encoding='utf-8') as f:
                    local_data = json.load(f)
                corrections = local_data.get("corrections", [])
                if corrections:
                    sheet.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context"])
                    rows_to_add = []
                    for c in corrections:
                        user_cls = c.get('user_classification', {})
                        ai_cls = c.get('ai_classification', {})
                        root = user_cls.get('root_word') or ai_cls.get('root_word') or ""
                        origin = user_cls.get('origin') or ai_cls.get('origin') or ""
                        pos = user_cls.get('pos') or ai_cls.get('pos') or ""
                        row = [c.get('timestamp', ''), c.get('original_word', ''), root, origin, pos, c.get('action', ''), c.get('context', '')]
                        rows_to_add.append(row)
                    if rows_to_add:
                        sheet.append_rows(rows_to_add)
                        st.success("✅ 이사 완료! 새로고침합니다.")
                        import time
                        time.sleep(1)
                        st.rerun()
        except: pass

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
        for _, row in deleted.tail(5).iterrows():
            prompt_lines.append(f"- 예외: '{row['original_word']}'는 분석 제외.")
        added = df[df['action'] == 'add']
        if not added.empty:
            missing_counts = added['original_word'].value_counts().head(5)
            for word, _ in missing_counts.items():
                prompt_lines.append(f"- 필수: '{word}'는 꼭 분석할 것.")
        modified = df[df['action'] == 'modify']
        for _, row in modified.tail(10).iterrows():
             prompt_lines.append(f"- 주의: '{row['original_word']}' -> 원형:'{row['root_word']}', 분류:'{row['origin']}'")
    if prompt_lines:
         return "\n[사용자 피드백]:\n" + "\n".join(prompt_lines) + "\n"
    return ""

def api_call_direct(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    data = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        if response.status_code == 200:
            result_json = response.json()
            if 'candidates' in result_json:
                text_res = result_json['candidates'][0]['content']['parts'][0]['text']
                json_match = re.search(r'\[.*\]', text_res, re.DOTALL)
                if json_match: return json.loads(json_match.group())
        return None
    except: return None

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
        return [w for w, p in pos if p in ['Noun', 'Verb', 'Adjective'] and len(w) > 1]
    except: return None

def get_analysis_hybrid(text, sheet_data):
    keywords = preprocess_with_morphology(text)
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    base_instruction = """
    국어학 전문가로서 문맥을 고려하여 실질 형태소(알맹이 단어)를 분석하세요.
    
    [절대 규칙]
    1. **조사(은/는/이/가/을/를 등), 어미(-다/-고/-면 등), 접사, 문장부호는 분석 결과에서 제외하세요.**
    2. '명사', '동사', '형용사', '부사', '관형사', '감탄사'만 추출하세요.
    3. '명사+하다'는 명사를 원형으로 함.
    4. 어원 분류: '고'(고유어), '한'(한자어), '외'(외래어), '혼'(혼종어)
    
    형식: [{"original_word": "단어", "root_word": "원형", "origin": "고", "pos": "명사"}]
    """
    
    if keywords:
        prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{text}"\n중점 분석 대상(힌트): {', '.join(keywords)}"""
    else:
        prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{text}" """
    return api_call_direct(prompt)

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
        return val.replace('🔵 ', '').replace('🟢 ', '').replace('🔴 ', '').replace('🟣 ', '').replace('📦 ', '').replace('🏃 ', '').replace('🎨 ', '').replace('⚡ ', '').replace('🔍 ', '').replace('❗ ', '').replace('✅ ', '').replace('⚠️ ', '')
    return val

def check_trust_level(root_word, uploaded_df):
    if uploaded_df is None or '자료' not in uploaded_df.columns:
        return False, 0
    match = uploaded_df[uploaded_df['자료'] == root_word]
    if match.empty:
        return False, 0
    try:
        count_val = match.iloc[0]['출연횟수']
        return (count_val >= TRUST_THRESHOLD), count_val
    except:
        return False, 0

def get_blacklist_from_sheet(sheet_data):
    blacklist = set()
    if not sheet_data: return blacklist
    for row in sheet_data:
        if row.get('action') == 'delete':
            blacklist.add(row.get('original_word'))
            blacklist.add(row.get('root_word'))
    return blacklist

# =========================================================
# 🖥️ 메인 화면
# =========================================================
sheet, sheet_data = get_sheet_data_cached()
if sheet:
    sync_json_to_sheet_if_empty(sheet, sheet_data)
    if not sheet_data: _, sheet_data = get_sheet_data_cached()

st.title("📝 국어활동 AI 분석기")

uploaded_df = None

with st.sidebar:
    st.header("📂 이어하기")
    uploaded_excel = st.file_uploader("작업하던 엑셀 파일", type=['xlsx'])
    if uploaded_excel:
        try: uploaded_df = pd.read_excel(uploaded_excel)
        except: pass
    
    if sheet: st.success(f"🌏 두뇌 연결됨 ({len(sheet_data)}건)")
    else: st.error("❌ 두뇌 연결 실패")
    
    # [누락 기능 1: 수동 추가]
    st.markdown("---")
    with st.expander("➕ AI가 놓친 단어 추가하기"):
        with st.form("manual_add_form"):
            add_orig = st.text_input("원본 단어 (예: 비빔냉면)")
            add_root = st.text_input("원형 (예: 비빔냉면)")
            add_origin = st.selectbox("분류", ["고", "한", "외", "혼"])
            add_pos = st.selectbox("품사", ["명사", "동사", "형용사", "부사", "관형사", "감탄사"])
            if st.form_submit_button("추가 및 학습"):
                if add_orig and add_root and sheet:
                    row = [datetime.now().isoformat(), add_orig, add_root, add_origin, add_pos, 'add', '수동추가']
                    sheet.append_row(row)
                    st.toast(f"✅ '{add_orig}' 학습 완료! 다시 분석하면 나옵니다.", icon="🎓")
                    st.rerun()

    # [이력 검색]
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

if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None

# 분석 로직
if analyze_btn and input_text:
    with st.spinner("AI가 분석 중입니다..."):
        raw_results = get_analysis_hybrid(input_text, sheet_data)
        
        if raw_results:
            validation_text = input_text.replace(" ", "")
            filtered_results = []
            
            POS_WHITELIST = ['명사', '동사', '형용사', '부사', '관형사', '감탄사', '수사', '대명사']
            blacklist = get_blacklist_from_sheet(sheet_data)
            
            for item in raw_results:
                original = item.get('original_word', '').replace(" ", "")
                root = item.get('root_word', '')
                pos = item.get('pos', '')
                
                if original not in validation_text: continue
                if not pos or pos not in POS_WHITELIST: continue
                if original in blacklist or root in blacklist: continue
                
                # [누락 기능 3: 하다 제거 2중 안전장치]
                if root.endswith("하다") and pos in ['명사', '동사', '형용사']:
                     root = root[:-2]
                     item['root_word'] = root

                if item.get('origin') == '순': item['origin'] = '고'
                
                is_trusted, count_val = check_trust_level(root, uploaded_df)
                
                if is_trusted:
                    item['status'] = '✅'
                    item['count'] = f"{count_val}회"
                else:
                    item['status'] = '🆕'
                    item['count'] = "신규"

                item['origin'] = add_emoji_to_origin(item.get('origin', ''))
                pos_map = {'명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사', '부사': '⚡ 부사', '관형사': '🔍 관형사', '감탄사': '❗ 감탄사'}
                item['pos'] = pos_map.get(pos, pos)
                
                filtered_results.append(item)
            st.session_state.analysis_result = filtered_results
        else:
            st.error("분석 결과가 없습니다.")

# 결과 화면
if st.session_state.analysis_result:
    st.markdown("### 📊 분석 결과 (수정 가능)")
    
    df_display = pd.DataFrame(st.session_state.analysis_result)
    
    # [누락 기능 2: 삭제 학습 옵션 추가]
    column_config = {
        "status": st.column_config.SelectboxColumn(
            "상태 (변경가능)", 
            options=["✅", "🆕", "⛔ 삭제(학습)"], 
            width="medium",
            help="'⛔ 삭제(학습)'을 선택하고 저장하면, 다음부터 이 단어는 나오지 않습니다."
        ),
        "count": st.column_config.TextColumn("빈도", width="small", disabled=True),
        "original_word": st.column_config.TextColumn("원본 단어", disabled=True),
        "root_word": "원형",
        "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
        "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "❗ 감탄사"])
    }
    
    cols = ["status", "count", "original_word", "root_word", "origin", "pos"]
    
    edited_df = st.data_editor(
        df_display[cols] if not df_display.empty else df_display,
        column_config=column_config, 
        use_container_width=True, 
        num_rows="dynamic",
        key="editor"
    )
    
    st.markdown("---")
    
    if st.button("📥 엑셀 파일 다운로드"):
        base_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])
        if uploaded_df is not None: base_df = uploaded_df.copy()

        for c in base_df.columns:
            if '쪽수' in c: base_df[c] = base_df[c].astype(object)
            
        new_rows = []
        saved_roots = set()
        
        current_data = edited_df.to_dict('records')
        cleaned_data = []
        learning_logs = []

        for item in current_data:
            # [삭제 학습 처리]
            if item['status'] == "⛔ 삭제(학습)":
                learning_logs.append({
                    'timestamp': datetime.now().isoformat(),
                    'original_word': item['original_word'],
                    'root_word': item['root_word'],
                    'origin': "",
                    'pos': "",
                    'action': 'delete',
                    'context': input_text
                })
                continue # 엑셀 저장에서는 제외

            item['origin'] = clean_value_for_save(item['origin'])
            item['pos'] = clean_value_for_save(item['pos'])
            cleaned_data.append(item)
            
            # [수정 학습 처리]
            # (단순화를 위해 모든 유효 데이터를 'modify'로 기록하여 강화 학습)
            learning_logs.append({
                'timestamp': datetime.now().isoformat(),
                'original_word': item['original_word'],
                'root_word': item['root_word'],
                'origin': item['origin'],
                'pos': item['pos'],
                'action': 'modify',
                'context': input_text
            })

        counts = Counter([x['root_word'] for x in cleaned_data])
        
        for item in cleaned_data:
            root = item['root_word']
            if root in saved_roots: continue
            saved_roots.add(root)
            cnt = counts[root]
            val = f"{page_num}_{cnt}" if cnt > 1 else page_num
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
        
        output_excel = io.BytesIO()
        with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
            base_df.to_excel(writer, index=False)
        output_excel.seek(0)
        
        st.download_button(
            label="파일 저장하기 (클릭)",
            data=output_excel,
            file_name="국어활동_분석결과_최종.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary"
        )
        
        # [구글 시트 일괄 저장]
        if sheet and learning_logs:
            try:
                rows_to_add = [list(log.values()) for log in learning_logs]
                sheet.append_rows(rows_to_add)
                st.toast(f"✅ 학습 완료: {len(rows_to_add)}건 (삭제 포함)", icon="🧠")
            except Exception as e:
                st.error(f"학습 저장 실패: {e}")