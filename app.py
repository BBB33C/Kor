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

# =========================================================
# ⚙️ 설정
# =========================================================
API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk" # 기존 키 유지
MODEL_NAME = "gemini-2.5-flash"
SHEET_NAME = "Korean_DB"  # ⚠️ 님이 만든 구글 시트 이름과 똑같아야 함!

st.set_page_config(page_title="국어활동 AI 분석기", page_icon="📝", layout="wide")

# =========================================================
# 🔐 구글 시트 연결 및 데이터 이사 (Migration)
# =========================================================
def get_google_sheet_data():
    """구글 시트와 연결하고 데이터를 가져옵니다."""
    try:
        # Streamlit Secrets에서 키 가져오기
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        
        # Secrets 데이터를 딕셔너리로 변환
        creds_dict = dict(st.secrets["gcp_service_account"])
        
        # [중요] TOML에서 줄바꿈 문자가 깨질 경우를 대비한 방어 코드
        if "private_key" in creds_dict:
             creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # 시트 열기
        sheet = client.open(SHEET_NAME).sheet1
        data = sheet.get_all_records()
        return sheet, data
    except Exception as e:
        # 시트 이름이 틀렸거나 권한이 없을 때
        st.error(f"❌ 구글 시트 연결 실패: {e}")
        st.info(f"💡 팁: '{SHEET_NAME}' 시트에 '{creds_dict.get('client_email')}' 계정을 '편집자'로 초대했는지 확인하세요.")
        return None, []

def sync_json_to_sheet_if_empty(sheet, current_data):
    """
    [핵심] 시트가 비어있으면, 기존 JSON 파일 내용을 업로드합니다. (최초 1회용)
    """
    if not sheet: return
    if len(current_data) > 0:
        return  # 시트에 이미 데이터가 있으면 패스 (이사 완료됨)

    json_path = "korean_analysis_learning.json"
    if os.path.exists(json_path):
        try:
            with st.spinner("📦 기존 지능(JSON)을 구글 시트로 이사하는 중입니다..."):
                with open(json_path, 'r', encoding='utf-8') as f:
                    local_data = json.load(f)
                
                corrections = local_data.get("corrections", [])
                
                if corrections:
                    # 1. 헤더 만들기 (첫 줄)
                    sheet.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context"])
                    
                    # 2. 데이터 변환
                    rows_to_add = []
                    for c in corrections:
                        # 데이터가 복잡하게 꼬여있을 수 있어 안전하게 가져오기
                        user_cls = c.get('user_classification', {})
                        ai_cls = c.get('ai_classification', {})
                        
                        # 사용자 수정본이 있으면 그거 쓰고, 없으면 AI 거 씀
                        root = user_cls.get('root_word') or ai_cls.get('root_word') or ""
                        origin = user_cls.get('origin') or ai_cls.get('origin') or ""
                        pos = user_cls.get('pos') or ai_cls.get('pos') or ""
                        
                        row = [
                            c.get('timestamp', ''),
                            c.get('original_word', ''),
                            root,
                            origin,
                            pos,
                            c.get('action', ''),
                            c.get('context', '')
                        ]
                        rows_to_add.append(row)
                    
                    # 3. 한 번에 업로드
                    if rows_to_add:
                        sheet.append_rows(rows_to_add)
                        st.success(f"✅ 이사 완료! {len(rows_to_add)}개의 데이터를 구글 시트로 옮겼습니다. 이제 JSON 파일은 없어도 됩니다.")
                        import time
                        time.sleep(2) # 성공 메시지 읽을 시간 줌
                        st.rerun() # 새로고침
        except Exception as e:
            st.error(f"이사 중 오류 발생: {e}")

# =========================================================
# 🧠 AI 학습 프롬프트 생성 (구글 시트 기반)
# =========================================================
def generate_prompt_from_sheet(sheet_data):
    """구글 시트 데이터를 읽어서 AI에게 가르칠 내용을 만듭니다."""
    if not sheet_data: return ""
    
    # 1. 시트 데이터를 데이터프레임으로 변환 (다루기 쉽게)
    df = pd.DataFrame(sheet_data)
    
    if df.empty: return ""

    prompt_lines = []
    
    # [규칙 1] 제외 단어 (action이 delete인 것)
    deleted = df[df['action'] == 'delete']
    for _, row in deleted.tail(5).iterrows(): # 최근 5개만
        prompt_lines.append(f"- 예외 처리: 문맥 '{row['context']}'에서 '{row['original_word']}'는 분석하지 말고 제외할 것.")

    # [규칙 2] 필수 단어 (action이 add인 것)
    added = df[df['action'] == 'add']
    missing_counts = added['original_word'].value_counts().head(5)
    for word, _ in missing_counts.items():
        prompt_lines.append(f"- 필수 포함: '{word}'는 분석에서 절대 누락하지 말 것.")

    # [규칙 3] 수정 사항 (action이 modify인 것)
    # original_word -> root_word 로 매핑 학습
    modified = df[df['action'] == 'modify']
    # 자주 틀리는 것 찾기 (같은 단어가 여러 번 수정되었으면 중요함)
    for _, row in modified.tail(10).iterrows():
         prompt_lines.append(f"- 주의: '{row['original_word']}'는 원형을 '{row['root_word']}', 분류를 '{row['origin']}', 품사를 '{row['pos']}'로 분석할 것.")

    if prompt_lines:
         return "\n[사용자 피드백 데이터(우선순위 높음)]:\n" + "\n".join(prompt_lines) + "\n"
    return ""

# =========================================================
# 🛠️ 기타 유틸리티 (기존과 동일)
# =========================================================
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
    
    # [변경] JSON 대신 구글 시트 데이터로 프롬프트 생성
    learning_prompt = generate_prompt_from_sheet(sheet_data)
    
    base_instruction = """
    국어학 전문가로서, 아래 문맥을 고려하여 '분석 대상' 단어들의 원형, 어원(고/한/외/혼), 품사를 분석하세요.
    규칙:
    1. 결과는 JSON 배열만 출력.
    2. '명사+하다'는 명사를 원형으로 함.
    3. 어원 분류: '고'(고유어), '한'(한자어), '외'(외래어), '혼'(혼종어)
    형식: [{"original_word": "단어", "root_word": "원형", "origin": "고", "pos": "명사"}]
    """
    
    if keywords:
        prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{text}"\n분석 대상 힌트: {', '.join(keywords)}"""
    else:
        prompt = f"""{learning_prompt}\n{base_instruction}\n다음 문장에서 명사, 동사, 형용사를 추출하여 분석하세요.\n문장: "{text}" """
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

# =========================================================
# 🖥️ 메인 화면 로직
# =========================================================

# 1. 구글 시트 연결 시도
sheet, sheet_data = get_google_sheet_data()

# 2. 최초 1회 이사 (JSON -> Sheet)
if sheet:
    sync_json_to_sheet_if_empty(sheet, sheet_data)
    # 이사 후 데이터가 갱신되었을 수 있으니 다시 읽기
    if not sheet_data: 
        sheet_data = sheet.get_all_records()

st.title("📝 국어활동 AI 분석기")
st.caption("Google Sheets 연동 버전")

# 사이드바
with st.sidebar:
    st.header("📂 이어하기 (선택)")
    st.markdown("지난번 작업하던 엑셀 파일이 있으면 넣으세요. (없어도 됨)")
    uploaded_excel = st.file_uploader("작업하던 엑셀 파일", type=['xlsx'])
    
    # 연결 상태 표시
    if sheet:
        st.success(f"🌏 구글 시트(두뇌) 연결됨\n데이터: {len(sheet_data)}건")
    else:
        st.error("❌ 구글 시트 연결 실패")

# 메인 입력
col1, col2 = st.columns([4, 1])
with col1:
    input_text = st.text_area("분석할 문장을 입력하세요", height=150, placeholder="예: 나는 어제 맛있는 비빔냉면을 먹었다.")
with col2:
    page_num = st.text_input("쪽수", value="1")
    analyze_btn = st.button("🚀 분석 실행", use_container_width=True)

if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None

# 분석 실행
if analyze_btn and input_text:
    with st.spinner("AI가 생각 중입니다..."):
        raw_results = get_analysis_hybrid(input_text, sheet_data)
        
        if raw_results:
            validation_text = input_text.replace(" ", "")
            filtered_results = []
            for item in raw_results:
                original = item.get('original_word', '').replace(" ", "")
                if original in validation_text:
                    if item.get('origin') == '순': item['origin'] = '고'
                    filtered_results.append(item)
            st.session_state.analysis_result = filtered_results
        else:
            st.error("분석 결과가 없습니다.")

# 결과 화면
if st.session_state.analysis_result:
    st.markdown("### 📊 분석 결과 (수정 가능)")
    
    df_display = pd.DataFrame(st.session_state.analysis_result)
    column_config = {
        "original_word": st.column_config.TextColumn("원본 단어", disabled=True),
        "root_word": "원형",
        "origin": st.column_config.SelectboxColumn("분류", options=["고", "한", "외", "혼"]),
        "pos": st.column_config.SelectboxColumn("품사", options=["명사", "동사", "형용사", "부사", "관형사", "감탄사"])
    }
    
    edited_df = st.data_editor(
        df_display, 
        column_config=column_config, 
        use_container_width=True, 
        num_rows="dynamic",
        key="editor"
    )
    
    st.markdown("---")
    
    if st.button("📥 엑셀 파일 다운로드"):
        # [방어 로직]
        base_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])
        
        if uploaded_excel:
            try:
                temp_df = pd.read_excel(uploaded_excel)
                if '자료' in temp_df.columns:
                    base_df = temp_df
                else:
                    st.toast("⚠️ 파일 양식 불일치. 새로 만듭니다.", icon="ℹ️")
            except:
                st.toast("⚠️ 파일 오류. 새로 만듭니다.", icon="ℹ️")

        for c in base_df.columns:
            if '쪽수' in c: base_df[c] = base_df[c].astype(object)
            
        new_rows = []
        saved_roots = set()
        current_data = edited_df.to_dict('records')
        counts = Counter([x['root_word'] for x in current_data])
        
        for item in current_data:
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