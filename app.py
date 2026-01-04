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

def get_sheet_data_fresh():
    client = get_google_sheet_client()
    if not client: return None, []
    try:
        sheet = client.open(SHEET_NAME).sheet1
        data = sheet.get_all_records()
        return sheet, data
    except: return None, []

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
        # [타임아웃 해결] 30초 -> 300초(5분)로 연장
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
        return [w for w, p in pos if p in ['Noun', 'Verb', 'Adjective'] and len(w) > 1]
    except: return None

def get_analysis_hybrid(text, sheet_data):
    keywords = preprocess_with_morphology(text)
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    base_instruction = """
    국어학 전문가로서 문맥을 고려하여 실질 형태소(알맹이 단어)를 분석하세요.
    [절대 규칙]
    1. 조사, 어미, 접사, 문장부호, 부사, 관형사, 감탄사는 제외하세요.
    2. 오직 '명사', '동사', '형용사'만 추출하세요.
    3. '명사+하다'는 명사를 원형으로 하되, 문맥을 고려하세요.
    4. 어원 분류: '고'(고유어), '한'(한자어), '외'(외래어), '혼'(혼종어)
    형식: [{"original_word": "단어", "root_word": "원형", "origin": "고", "pos": "명사"}]
    """
    
    if keywords:
        prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{text}"\n힌트: {', '.join(keywords)}"""
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
sheet, sheet_data = get_sheet_data_fresh()
if sheet:
    sync_json_to_sheet_if_empty(sheet, sheet_data)
    if not sheet_data: _, sheet_data = get_sheet_data_fresh()

st.title("📝 국어활동 AI 분석기")

# [세션 상태 초기화]
if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None
if 'excel_buffer' not in st.session_state:
    st.session_state.excel_buffer = None
# [누적 저장] 작업을 위한 마스터 데이터프레임
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
             # [누적 저장] 파일을 새로 올리면 마스터 데이터도 이걸로 초기화
             if st.session_state.master_df is None:
                 st.session_state.master_df = uploaded_df.copy()
        else:
             st.caption("ℹ️ 빈 파일 혹은 양식이 다른 파일입니다.")
    else:
        # 파일이 없으면 빈 마스터 생성 (최초 1회)
        if st.session_state.master_df is None:
            st.session_state.master_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])

    if sheet: st.success(f"🌏 두뇌 연결됨 ({len(sheet_data)}건)")
    else: st.error("❌ 두뇌 연결 실패")
    
    st.markdown("---")
    
    # [수정] 수동 추가 시 즉시 화면 리스트에 반영
    with st.expander("➕ AI가 놓친 단어 추가하기"):
        with st.form("manual_add_form"):
            add_orig = st.text_input("원본 단어")
            add_root = st.text_input("원형")
            add_origin = st.selectbox("분류", ["고", "한", "외", "혼"])
            add_pos = st.selectbox("품사", ["명사", "동사", "형용사"]) 
            if st.form_submit_button("추가 및 학습"):
                if add_orig and add_root and sheet:
                    # 1. 시트에 저장
                    row = [datetime.now().isoformat(), add_orig, add_root, add_origin, add_pos, 'add', '수동추가']
                    sheet.append_row(row)
                    
                    # 2. 현재 화면 리스트에 강제 주입
                    if st.session_state.analysis_result is not None:
                        formatted_pos = {'명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사'}.get(add_pos, add_pos)
                        new_item = {
                            'original_word': add_orig, 'root_word': add_root,
                            'origin': add_emoji_to_origin(add_origin), 'pos': formatted_pos,
                            'status': '✅ 수동', 'count': '1회', 'delete_check': False
                        }
                        st.session_state.analysis_result.append(new_item)
                    
                    st.toast(f"✅ '{add_orig}' 추가 완료! 리스트에 즉시 반영됩니다.", icon="🎓")
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

# 분석 실행
if analyze_btn and input_text:
    with st.spinner("AI가 분석 중입니다..."):
        raw_results = get_analysis_hybrid(input_text, sheet_data)
        
        if raw_results:
            validation_text = input_text.replace(" ", "")
            filtered_results = []
            
            POS_WHITELIST = ['명사', '동사', '형용사'] 
            blacklist = get_blacklist_from_sheet(sheet_data)
            problematic_words = get_problematic_words(sheet_data)
            
            all_roots = []
            valid_items = []
            
            for item in raw_results:
                original = item.get('original_word', '').replace(" ", "")
                root = item.get('root_word', '')
                pos = item.get('pos', '')
                
                if original not in validation_text: continue
                if not pos or pos not in POS_WHITELIST: continue
                if original in blacklist or root in blacklist: continue
                
                if item.get('origin') == '순': item['origin'] = '고'
                item['origin'] = add_emoji_to_origin(item.get('origin', ''))
                pos_map = {'명사': '📦 명사', '동사': '🏃 동사', '형용사': '🎨 형용사'}
                item['pos'] = pos_map.get(pos, pos)
                
                is_trusted = check_trust_level_strict(root, st.session_state.master_df, problematic_words)
                
                if is_trusted: item['status'] = '✅ 자동' 
                else: item['status'] = '📝 검토' 
                
                valid_items.append(item)
                all_roots.append(root)
            
            root_counts = Counter(all_roots)
            for item in valid_items:
                item['delete_check'] = False
                cnt = root_counts[item['root_word']]
                item['count'] = f"{cnt}회"
                filtered_results.append(item)
                
            st.session_state.analysis_result = filtered_results
        else: pass

# 결과 화면
if st.session_state.analysis_result:
    st.markdown("### 📊 분석 결과 (수정 및 삭제)")
    
    df_display = pd.DataFrame(st.session_state.analysis_result)
    
    column_config = {
        "delete_check": st.column_config.CheckboxColumn("삭제", width="small"),
        "status": st.column_config.TextColumn("상태", width="medium", disabled=True),
        "count": st.column_config.TextColumn("빈도(현재)", width="small", disabled=True),
        "original_word": st.column_config.TextColumn("원본 단어", disabled=True),
        "root_word": "원형",
        "origin": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
        "pos": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사"])
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
                        st.toast(f"🗑️ {len(rows_to_add)}개 단어 삭제 학습 완료! 1초 후 반영됩니다.", icon="✅")
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
        # [누적 저장 로직] 버튼 클릭 시 마스터 DB에 병합
        if st.button("💾 엑셀에 저장 (누적 저장)", type="primary"):
            final_data = edited_df[edited_df['delete_check'] == False].to_dict('records')
            
            # 1. 마스터 DF 가져오기 (없으면 생성)
            base_df = st.session_state.master_df
            if base_df is None:
                 base_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])

            # 2. 쪽수 컬럼 포맷 확인
            for c in base_df.columns:
                if '쪽수' in c: base_df[c] = base_df[c].astype(object)
                
            new_rows = []
            saved_roots = set()
            cleaned_data_for_excel = []
            learning_logs = []

            for item in final_data:
                item['origin'] = clean_value_for_save(item['origin'])
                item['pos'] = clean_value_for_save(item['pos'])
                cleaned_data_for_excel.append(item)
                
                learning_logs.append({
                    'timestamp': datetime.now().isoformat(),
                    'original_word': item['original_word'],
                    'root_word': item['root_word'],
                    'origin': item['origin'],
                    'pos': item['pos'],
                    'action': 'modify',
                    'context': input_text
                })

            counts = Counter([x['root_word'] for x in cleaned_data_for_excel])
            
            for item in cleaned_data_for_excel:
                root = item['root_word']
                if root in saved_roots: continue
                saved_roots.add(root)
                
                cnt = counts[root]
                val = f"{page_num}_{cnt}" if cnt > 1 else page_num
                origin_val = item.get('origin', '고')
                
                # 기존 데이터에 병합 (기존에 있으면 쪽수 추가, 없으면 행 추가)
                if root in base_df['자료'].values:
                    idx = base_df[base_df['자료'] == root].index[0]
                    # 이미 있는 쪽수 컬럼 중 값이 있는 것의 개수를 세어 다음 칸에 넣음
                    filled = base_df.loc[idx].filter(like='쪽수').notna().sum()
                    col = f"쪽수{filled+1}"
                    if col not in base_df.columns: base_df[col] = float('nan')
                    base_df.at[idx, col] = val
                else:
                    new_rows.append({'구분': origin_val, '자료': root, '쪽수1': val})
            
            if new_rows:
                base_df = pd.concat([base_df, pd.DataFrame(new_rows)], ignore_index=True)
            
            # 3. 계산 및 정렬 업데이트
            base_df['출연횟수'] = base_df.apply(calculate_total_appearances, axis=1)
            base_df['sort'] = base_df['구분'].map({'고':1, '순':1, '한':2, '외':3, '혼':4}).fillna(5)
            base_df = base_df.sort_values(['sort', '자료']).drop('sort', axis=1)
            
            # 4. 마스터 DF 업데이트 (세션 저장)
            st.session_state.master_df = base_df
            
            # 5. 다운로드용 버퍼 생성
            output_excel = io.BytesIO()
            with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
                base_df.to_excel(writer, index=False)
            output_excel.seek(0)
            
            st.session_state.excel_buffer = output_excel
            
            if sheet and learning_logs:
                try:
                    rows_to_add = [list(log.values()) for log in learning_logs]
                    sheet.append_rows(rows_to_add)
                    st.toast(f"✅ 학습 완료: {len(rows_to_add)}건 저장됨.", icon="🧠")
                except Exception as e: pass
            
            st.success("✅ 누적 저장 완료! (1, 2, ... 쪽 내용이 모두 합쳐졌습니다.)")

    if st.session_state.excel_buffer:
        st.download_button(
            label="📥 엑셀파일 다운로드하기 (전체 내용)",
            data=st.session_state.excel_buffer,
            file_name="국어활동_분석결과_통합.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="secondary"
        )