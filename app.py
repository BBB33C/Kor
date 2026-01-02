import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import os
from collections import Counter
from datetime import datetime

# =========================================================
# ⚙️ 설정 및 API 키 (기존 키 유지)
# =========================================================
API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk"  # 님의 API 키
MODEL_NAME = "gemini-2.5-flash"
TRUST_PAGE_COUNT = 3

# Streamlit 페이지 설정
st.set_page_config(
    page_title="국어활동 AI 분석기 (Web)",
    page_icon="📝",
    layout="wide"
)

# =========================================================
# 🧠 1. 핵심 로직 (두뇌) - 기존 코드 이식
# =========================================================

# 형태소 분석기 (KoNLPy) - 웹 환경 고려하여 예외처리 강화
try:
    from konlpy.tag import Okt
    MORPHOLOGY_AVAILABLE = True
except:
    MORPHOLOGY_AVAILABLE = False

def load_learning_data():
    """학습 데이터(JSON)를 읽어옵니다."""
    # 1. 사용자가 직접 업로드한 경우
    if 'uploaded_json' in st.session_state and st.session_state.uploaded_json is not None:
        return json.loads(st.session_state.uploaded_json.getvalue().decode('utf-8'))
    
    # 2. 깃허브(같은 폴더)에 있는 파일 읽기
    default_path = "korean_analysis_learning.json"
    if os.path.exists(default_path):
        with open(default_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    # 3. 아무것도 없으면 빈 깡통 반환
    return {"corrections": []}

def generate_learning_prompt(learning_data):
    """학습 데이터를 바탕으로 AI에게 보낼 프롬프트를 만듭니다."""
    corrections = learning_data.get("corrections", [])
    if not corrections: return ""
    
    correction_counts = Counter()
    missing_words = Counter()
    deleted_examples = []

    for c in corrections:
        if c['action'] == 'modify':
            ai_root = c['ai_classification'].get('root_word', '')
            user_root = c['user_classification'].get('root_word', '')
            if ai_root and user_root and ai_root != user_root:
                correction_counts[(ai_root, user_root)] += 1
        elif c['action'] == 'add':
            missing_words[c['original_word']] += 1
        elif c['action'] == 'delete':
            ctx = c.get('context', '')
            word = c.get('original_word', '')
            if ctx and word:
                deleted_examples.append(f"- 문장: '{ctx}' -> 제외: '{word}'")
    
    prompt_lines = []
    for (bad, good), count in correction_counts.most_common(3):
        prompt_lines.append(f"- 주의: '{bad}'(X) -> '{good}'(O)으로 분석할 것.")
    for word, count in missing_words.most_common(3):
        prompt_lines.append(f"- 필수 포함: '{word}'는 분석에서 누락하지 말 것.")
    for example in deleted_examples[-3:]:
        prompt_lines.append(example)

    if prompt_lines:
        return "\n[사용자 피드백(반드시 준수)]:\n" + "\n".join(prompt_lines) + "\n"
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
    except Exception as e:
        st.error(f"API 통신 오류: {e}")
        return None

def preprocess_with_morphology(text):
    if not MORPHOLOGY_AVAILABLE: return None
    try:
        okt = Okt()
        pos = okt.pos(text, stem=True)
        return [w for w, p in pos if p in ['Noun', 'Verb', 'Adjective'] and len(w) > 1]
    except:
        return None

def get_analysis_hybrid(text, learning_data):
    keywords = preprocess_with_morphology(text)
    learning_prompt = generate_learning_prompt(learning_data)
    
    base_instruction = """
    국어학 전문가로서, 아래 문맥을 고려하여 '분석 대상' 단어들의 원형, 어원(고/한/외/혼), 품사를 분석하세요.
    규칙:
    1. 결과는 JSON 배열만 출력.
    2. '명사+하다'는 명사를 원형으로 함.
    3. 어원 분류: '고'(고유어), '한'(한자어), '외'(외래어), '혼'(혼종어)
    형식: [{"original_word": "단어", "root_word": "원형", "origin": "고", "pos": "명사"}]
    """
    
    # 전략: 키워드가 있으면 힌트로 주고, 없으면 그냥 분석
    if keywords:
        prompt = f"""{learning_prompt}\n{base_instruction}\n문장: "{text}"\n분석 대상 힌트: {', '.join(keywords)}"""
    else:
        prompt = f"""{learning_prompt}\n{base_instruction}\n다음 문장에서 명사, 동사, 형용사를 추출하여 분석하세요.\n문장: "{text}" """
        
    return api_call_direct(prompt)

def calculate_total_appearances(row):
    """엑셀 쪽수 계산 로직"""
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
# 🖥️ 2. 웹 화면 구성 (UI)
# =========================================================

st.title("📝 국어활동 AI 분석기 (Web Ver.)")

# [사이드바] 파일 관리
with st.sidebar:
    st.header("📂 파일 관리")
    
    st.markdown("### 1. 엑셀 파일 (Database)")
    uploaded_excel = st.file_uploader("기존 분석 엑셀 파일 업로드", type=['xlsx'])
    
    st.markdown("### 2. 학습 데이터 (Brain)")
    uploaded_json = st.file_uploader("korean_analysis_learning.json 업로드", type=['json'], key="uploaded_json")
    
    if uploaded_json:
        st.success("✅ 학습 데이터를 로드했습니다!")
    elif os.path.exists("korean_analysis_learning.json"):
        st.info("✅ 서버에 있는 학습 데이터를 사용합니다.")
    else:
        st.warning("⚠️ 학습 데이터가 없습니다. 깡통 상태입니다.")

# [메인] 입력 및 분석
col1, col2 = st.columns([4, 1])
with col1:
    input_text = st.text_area("분석할 문장을 입력하세요", height=100, placeholder="예: 나는 어제 맛있는 비빔냉면을 먹었다.")
with col2:
    page_num = st.text_input("쪽수", value="1")
    analyze_btn = st.button("🚀 분석 실행", use_container_width=True)

# [세션 상태 관리] 분석 결과 임시 저장
if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None

# 분석 실행 로직
if analyze_btn and input_text:
    with st.spinner("AI가 분석 중입니다..."):
        # 1. 학습 데이터 로드
        learning_data = load_learning_data()
        
        # 2. 분석 수행
        raw_results = get_analysis_hybrid(input_text, learning_data)
        
        if raw_results:
            # 3. 유령 단어 필터링 (간소화)
            validation_text = input_text.replace(" ", "")
            filtered_results = []
            for item in raw_results:
                original = item.get('original_word', '').replace(" ", "")
                # 원본 텍스트에 단어가 실제로 있는지 검증
                if original in validation_text:
                    if item.get('origin') == '순': item['origin'] = '고'
                    filtered_results.append(item)
            
            st.session_state.analysis_result = filtered_results
        else:
            st.error("분석 결과가 없습니다. 다시 시도해주세요.")

# 결과 화면 및 수정 (Data Editor 사용)
if st.session_state.analysis_result:
    st.markdown("### 📊 분석 결과 확인 및 수정")
    st.info("아래 표에서 내용을 직접 수정할 수 있습니다. 수정한 뒤 '엑셀 다운로드'를 누르세요.")
    
    # 데이터프레임으로 변환하여 에디터로 보여줌
    df_display = pd.DataFrame(st.session_state.analysis_result)
    
    # 컬럼 순서 및 한글화
    column_config = {
        "original_word": st.column_config.TextColumn("원본 단어", disabled=True), # 원본은 수정 불가
        "root_word": "원형",
        "origin": st.column_config.SelectboxColumn("분류", options=["고", "한", "외", "혼"]),
        "pos": st.column_config.SelectboxColumn("품사", options=["명사", "동사", "형용사", "부사", "관형사", "감탄사"])
    }
    
    edited_df = st.data_editor(
        df_display, 
        column_config=column_config, 
        use_container_width=True, 
        num_rows="dynamic", # 행 추가/삭제 가능
        key="editor"
    )
    
    # =========================================================
    # 💾 3. 저장 및 다운로드 로직
    # =========================================================
    st.markdown("---")
    st.subheader("📥 결과 저장")
    
    # 엑셀 처리
    if st.button("엑셀 파일 생성하기"):
        # 1. 기존 엑셀 읽기 (없으면 새로 생성)
        if uploaded_excel:
            base_df = pd.read_excel(uploaded_excel)
        else:
            base_df = pd.DataFrame(columns=['구분', '자료', '출연횟수'])
            
        # 2. 쪽수 컬럼 데이터 타입 정리
        for c in base_df.columns:
            if '쪽수' in c: base_df[c] = base_df[c].astype(object)
            
        # 3. 데이터 병합 로직 (기존 코드 이식)
        new_rows = []
        saved_roots = set()
        
        # 편집된 데이터(edited_df)를 기반으로 작업
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
                # 이미 있는 단어면 쪽수 추가
                idx = base_df[base_df['자료'] == root].index[0]
                filled = base_df.loc[idx].filter(like='쪽수').notna().sum()
                col = f"쪽수{filled+1}"
                if col not in base_df.columns: base_df[col] = float('nan')
                base_df.at[idx, col] = val
            else:
                # 없는 단어면 신규 추가
                new_rows.append({'구분': origin_val, '자료': root, '쪽수1': val})
        
        if new_rows:
            base_df = pd.concat([base_df, pd.DataFrame(new_rows)], ignore_index=True)
            
        # 4. 출연횟수 재계산 및 정렬
        base_df['출연횟수'] = base_df.apply(calculate_total_appearances, axis=1)
        base_df['sort'] = base_df['구분'].map({'고':1, '순':1, '한':2, '외':3, '혼':4}).fillna(5)
        base_df = base_df.sort_values(['sort', '자료']).drop('sort', axis=1)
        
        # 5. 다운로드 버튼 생성 (BytesIO 사용)
        output_excel = io.BytesIO()
        with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
            base_df.to_excel(writer, index=False)
        output_excel.seek(0)
        
        st.success("✅ 파일 처리가 완료되었습니다!")
        st.download_button(
            label="📊 업데이트된 엑셀 다운로드",
            data=output_excel,
            file_name="국어활동_분석결과_최종.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        
        # 6. (선택사항) 학습 데이터 업데이트 및 다운로드
        # 웹에서는 자동 저장이 안 되므로, 변경된 학습 데이터를 다운로드 받게 해줍니다.
        # 이 부분은 복잡도를 줄이기 위해, 사용자가 수정한 내용이 있으면
        # "나중에 덮어씌울 json 파일"을 뱉어주는 기능으로 구현 가능합니다.
        # 필요하시면 말씀해주세요! (현재는 엑셀 저장에 집중)