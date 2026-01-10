import streamlit as st
import pandas as pd
import requests
import json
import re
import io
import base64
import traceback
from oauth2client.service_account import ServiceAccountCredentials
from collections import Counter
from datetime import datetime
import time

# =========================================================
# ⚙️ [핵심 설정 변경] 모델 변경 및 안전장치 해제
# =========================================================
try:
    if "GEMINI_API_KEY" in st.secrets:
        API_KEY = st.secrets["GEMINI_API_KEY"]
    else:
        API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk" 
except:
    API_KEY = "AIzaSyCC6oyL7POpbWq2FZrJ2zIJuiosupFQZYk"

# [변경 1] 실험 버전(2.0) 대신 안정적인 1.5 Flash 사용
MODEL_NAME = "gemini-1.5-flash" 
SHEET_NAME = "Korean_DB" 
TRUST_THRESHOLD = 3 

st.set_page_config(page_title="AI 분석기 (Deep Debug)", page_icon="🛠️", layout="wide")

# =========================================================
# 🔐 구글 시트 연결 (기존 유지)
# =========================================================
@st.cache_resource
def get_google_sheet_client():
    try:
        if "gcp_service_account" not in st.secrets: return None
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict: creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
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
        sheet = spreadsheet.worksheet(target_sheet_name)
        return sheet, sheet.get_all_records()
    except: return None, []

# =========================================================
# 🧠 AI 통신 로직 (여기가 핵심 수정됨)
# =========================================================
def api_call_direct(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    
    # [변경 2] 안전 필터 강제 해제 (BLOCK_NONE)
    # AI가 텍스트를 검열해서 빈 응답을 보내는 것을 막습니다.
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
    ]

    data = {
        "contents": [{"parts": [{"text": prompt}]}], 
        "safetySettings": safety_settings,
        "generationConfig": {"temperature": 0.1}
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        # 1. HTTP 상태 코드 확인
        if response.status_code != 200:
            st.error(f"❌ HTTP 오류: {response.status_code}")
            st.text(response.text)
            return None

        result_json = response.json()
        
        # [변경 3] 구글이 보낸 '진짜 응답'을 화면에 까발리기
        with st.expander("🔍 구글 AI 원본 응답 (클릭해서 확인)", expanded=True):
            st.json(result_json)

        # 2. 응답 구조 확인
        if 'candidates' not in result_json:
            st.error("❌ candidates 키가 없습니다. (API 호출은 성공했으나 내용은 없음)")
            return None
            
        candidate = result_json['candidates'][0]
        
        # 3. 차단 여부 확인 (Finish Reason)
        if candidate.get('finishReason') != 'STOP':
            st.warning(f"⚠️ 비정상 종료: {candidate.get('finishReason')} (안전 필터 등)")
        
        # 4. 텍스트 추출 시도
        if 'content' not in candidate or 'parts' not in candidate['content']:
            st.error("❌ 텍스트 내용이 비어있습니다. (Parts missing)")
            return None
            
        text_res = candidate['content']['parts'][0]['text']
        
        # 5. JSON 파싱
        json_match = re.search(r'\[.*\]', text_res, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        else:
            st.error("❌ JSON 형식 찾기 실패")
            st.text(f"받은 텍스트: {text_res}")
            return None
            
    except Exception as e:
        st.error(f"❌ 파이썬 내부 오류: {str(e)}")
        st.code(traceback.format_exc())
        return None

def split_text_smartly(text, chunk_size=1000):
    return [text] # 디버깅을 위해 쪼개지 않고 통으로 보냄

def get_analysis_hybrid(text, sheet_data, mode_key):
    # 프롬프트 구성 (기존과 동일하되 명확하게)
    role = "당신은 국어학 분석가입니다. 문장에서 단어를 추출하여 분석하세요."
    rule = "결과는 반드시 JSON 리스트 포맷으로만 출력하세요. 예: [{\"original_word\": \"학교\", \"root_word\": \"학교\", \"origin\": \"한\", \"pos\": \"명사\"}]"
    prompt = f"{role}\n{rule}\n\n분석할 문장:\n{text}"
    
    return api_call_direct(prompt)

# =========================================================
# 🖥️ 화면 구성 (UI 단순화 -> 디버깅 집중)
# =========================================================
st.title("🛠️ 초정밀 디버깅 모드")

st.info("이 모드는 UI를 최소화하고, AI가 뱉는 데이터를 있는 그대로 보여줍니다.")

# 1. 텍스트 입력
txt_input = st.text_area("분석할 텍스트 입력", "안녕하세요 학교에 가려고 합니다", height=100)

if st.button("🚀 분석 시작 (Run Debug)", type="primary"):
    if not txt_input:
        st.warning("텍스트를 입력하세요.")
    else:
        st.write("---")
        st.write(f"📡 분석 요청 시작: {len(txt_input)}글자")
        
        # 분석 실행
        result = get_analysis_hybrid(txt_input, [], "SOUTH")
        
        st.write("---")
        if result:
            st.success("✅ 분석 성공! (결과 파싱 완료)")
            st.dataframe(result)
        else:
            st.error("❌ 분석 실패 (위의 '구글 AI 원본 응답'을 확인하세요)")