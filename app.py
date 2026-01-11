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
import traceback
from PIL import Image # 이미지 검증 및 재인코딩용 필수 라이브러리

# =========================================================
# [0] 기본 설정 및 상태 초기화
# =========================================================
st.set_page_config(
    page_title="국어활동 AI 분석기", 
    page_icon="📚", 
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 세션 상태 초기화 (모든 작업 상태 보존)
if 'step' not in st.session_state: st.session_state.step = 0
if 'mode_key' not in st.session_state: st.session_state.mode_key = None
if 'master_df' not in st.session_state: st.session_state.master_df = None
if 'analysis_result' not in st.session_state: st.session_state.analysis_result = []
if 'initial_draft' not in st.session_state: st.session_state.initial_draft = []
if 'file_bytes' not in st.session_state: st.session_state.file_bytes = None
if 'file_type' not in st.session_state: st.session_state.file_type = None
if 'page_idx' not in st.session_state: st.session_state.page_idx = 0
if 'total_pages' not in st.session_state: st.session_state.total_pages = 0
if 'start_offset' not in st.session_state: st.session_state.start_offset = 1
if 'extracted_text' not in st.session_state: st.session_state.extracted_text = ""
if 'debug_mode' not in st.session_state: st.session_state.debug_mode = False
if 'last_raw_response' not in st.session_state: st.session_state.last_raw_response = ""
if 'debug_log' not in st.session_state: st.session_state.debug_log = ""
if 'current_tab_idx' not in st.session_state: st.session_state.current_tab_idx = 0 
if 'is_finished' not in st.session_state: st.session_state.is_finished = False

# =========================================================
# [1] 디자인: CSS 매직
# =========================================================
if st.session_state.step == 0:
    st.markdown("""
        <style>
            .stApp { background-color: #0e1117 !important; color: #ffffff !important; }
            div.block-container div[data-testid="column"] div.stButton > button {
                width: 100%; height: 320px;
                background-color: #262730 !important;
                border: 2px solid rgba(255,255,255,0.05) !important;
                border-radius: 20px !important;
                color: #eeeeee !important;
                font-size: 1.5rem !important; font-weight: 700 !important;
                transition: all 0.3s ease !important;
                box-shadow: 0 4px 10px rgba(0,0,0,0.3) !important;
                white-space: pre-wrap !important;
                line-height: 1.4 !important;
            }
            div.block-container div[data-testid="column"] div.stButton > button:hover {
                transform: translateY(-8px);
                background-color: #2b2c36 !important;
            }
        </style>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
        <style>
            .stApp { background-color: #0e1117 !important; color: #ffffff !important; }
            .stTextArea textarea { font-family: 'Malgun Gothic', sans-serif !important; font-size: 16px !important; line-height: 1.6 !important; }
            .stButton button { border-radius: 8px; font-weight: bold; height: auto; }
            div.stRadio > div[role="radiogroup"] { display: flex; flex-direction: row; gap: 10px; }
            .control-card { background-color: #1e2129; padding: 20px; border-radius: 15px; border: 1px solid #3d4251; margin-bottom: 20px; }
            .status-badge { background-color: #2979ff; padding: 4px 12px; border-radius: 20px; font-size: 0.9rem; font-weight: 600; color: white !important; }
            .info-card { background-color: rgba(41, 121, 255, 0.1); border-left: 5px solid #2979ff; padding: 15px; border-radius: 5px; margin-top: 15px; }
            .debug-box { background-color: #000; color: #0f0; font-family: monospace; padding: 10px; border-radius: 5px; font-size: 0.8rem; overflow-x: auto; }
            /* 로딩 중 버튼 비활성화 효과 */
            button:disabled { opacity: 0.5 !important; cursor: not-allowed !important; }
        </style>
    """, unsafe_allow_html=True)

# 라이브러리 로드 확인
try:
    import pdfplumber
    PLUMBER_AVAILABLE = True
except ImportError:
    PLUMBER_AVAILABLE = False
try:
    import fitz 
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

# =========================================================
# [2] 구글 시트 및 API 유틸리티
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
            ws.append_row(["timestamp", "original_word", "root_word", "origin", "pos", "action", "context", "initial_root", "initial_origin"])
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
# [3] 데이터 병합 및 비교 학습 엔진
# =========================================================
def clean_val_for_save(v):
    if isinstance(v, str): 
        chars_to_remove = ['🔵 ', '🟢 ', '🔴 ', '🟣 ', '📦 ', '🏃 ', '🎨 ', '⚡ ', '🔍 ', '👤 ', '❗ ']
        for char in chars_to_remove: v = v.replace(char, '')
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
            if pd.notna(val) and str(val).strip() not in ['nan', '', 'None']: pages.append(str(val))
        for c in page_cols_new:
            val = row.get(f"{c}_new", row.get(c))
            if pd.notna(val) and str(val).strip() not in ['nan', '', 'None']: pages.append(str(val))
            
        unique_pages = sorted(list(set(pages)))
        for i, p in enumerate(unique_pages): new_row[f"쪽수{i+1}"] = p
        final_rows.append(new_row)
        
    result_df = pd.DataFrame(final_rows)
    result_df['출연횟수'] = result_df.apply(calc_freq, axis=1)
    sort_map = {'고':1, '순':1, '한':2, '외':3, '혼':4}
    result_df['sk'] = result_df['구분'].map(sort_map).fillna(5)
    result_df = result_df.sort_values(['sk', '자료']).drop('sk', axis=1)
    return result_df

def save_logic_with_learning():
    """비교 학습 엔진 로직"""
    sheet, _ = get_sheet_data_fresh(st.session_state.mode_key)
    now = datetime.now().isoformat()
    learning_logs = []
    
    final_results = pd.DataFrame(st.session_state.analysis_result)
    initial_draft = pd.DataFrame(st.session_state.initial_draft)
    
    # 1. 교정 및 삭제 학습
    for _, draft_row in initial_draft.iterrows():
        orig = draft_row['원본']
        match = final_results[final_results['원본'] == orig]
        
        if match.empty or match.iloc[0]['삭제']:
            learning_logs.append([now, orig, draft_row['원형'], draft_row['분류'], draft_row['품사'], 'delete', 'Engine-Compare', draft_row['원형'], draft_row['분류']])
        else:
            final_row = match.iloc[0]
            if (draft_row['원형'] != final_row['원형'] or 
                clean_val_for_save(draft_row['분류']) != clean_val_for_save(final_row['분류']) or 
                clean_val_for_save(draft_row['품사']) != clean_val_for_save(final_row['품사'])):
                
                learning_logs.append([
                    now, orig, final_row['원형'], 
                    clean_val_for_save(final_row['분류']), 
                    clean_val_for_save(final_row['품사']), 
                    'modify', 'Engine-Compare', 
                    draft_row['원형'], draft_row['분류']
                ])
                
    # 2. 추가 학습
    draft_originals = initial_draft['원본'].tolist()
    for _, final_row in final_results.iterrows():
        if final_row['원본'] not in draft_originals and not final_row['삭제']:
            learning_logs.append([
                now, final_row['원본'], final_row['원형'], 
                clean_val_for_save(final_row['분류']), 
                clean_val_for_save(final_row['품사']), 
                'add', 'Engine-New', '', ''
            ])
            
    if learning_logs: send_data_with_retry(sheet, learning_logs, True)
    
    # 마스터 데이터 업데이트 및 쪽수 계산
    valid = final_results[final_results['삭제']==False].copy()
    valid['n_cnt'] = valid['횟수'].apply(lambda x: int(re.sub(r'[^0-9]', '', str(x))) if re.search(r'\d', str(x)) else 1)
    agg = valid.groupby(['원형', '분류', '품사'], as_index=False).agg({'n_cnt': 'sum'})
    
    # 시작 쪽수 반영 자동 계산 로직
    p_num = str(st.session_state.page_idx + st.session_state.start_offset)
    temp_rows = []
    for _, item in agg.iterrows():
        val = f"{p_num}_{item['n_cnt']}" if item['n_cnt'] > 1 else p_num
        temp_rows.append({'구분': clean_val_for_save(item['분류']), '자료': item['원형'], '쪽수1': val})
        
    st.session_state.master_df = merge_master_data(st.session_state.master_df, pd.DataFrame(temp_rows))
    save_backup_to_cloud(st.session_state.mode_key, st.session_state.master_df)

# =========================================================
# [4] AI 분석 및 이미지 최적화 처리 (오류 해결 핵심 로직)
# =========================================================
def clean_raw_text(text):
    text = re.sub(r'.*\.indd.*', '', text)
    text = re.sub(r'\d{4}-\d{2}-\d{2}', '', text)
    text = re.sub(r'(오전|오후)\s+\d{1,2}:\d{2}:\d{2}', '', text)
    text = re.sub(r'본문\d?\(.*\)\d{2}', '', text)
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return '\n'.join(lines)

def process_image_for_api(image_bytes):
    """[해결책] 이미지를 명시적으로 PIL PNG로 변환하여 identify 오류 해결"""
    if not image_bytes: return None
    try:
        # 바이트 데이터를 바이너리 스트림으로 감싸서 열기
        img_io = io.BytesIO(image_bytes)
        img = Image.open(img_io)
        
        # RGB 모드로 강제 변환 (RGBA 등 멀티채널 오류 방지)
        if img.mode != "RGB":
            img = img.convert("RGB")
        
        # 해상도 최적화 (Gemini API 입력 규격 준수)
        if max(img.size) > 2000:
            img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
            
        output = io.BytesIO()
        img.save(output, format="PNG")
        return output.getvalue()
    except Exception as e:
        if "debug_log" in st.session_state:
            st.session_state.debug_log += f"이미지 전처리 경고 (무시됨): {str(e)}\n"
        return None

def api_call_direct(prompt, image_bytes=None):
    if not API_KEY: return None, "API Key Missing"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY.strip()}"
    headers = {'Content-Type': 'application/json'}
    
    parts = [{"text": prompt}]
    if image_bytes:
        optimized_img = process_image_for_api(image_bytes)
        if optimized_img:
            try:
                b64_img = base64.b64encode(optimized_img).decode('utf-8')
                parts.append({"inline_data": {"mime_type": "image/png", "data": b64_img}})
            except: pass
            
    try:
        res = requests.post(url, headers=headers, json={"contents": [{"parts": parts}]}, timeout=300)
        if res.status_code == 200: 
            text_res = res.json()['candidates'][0]['content']['parts'][0]['text']
            return text_res, "Success"
        return None, f"Error: {res.status_code} - {res.text}"
    except Exception as e: 
        return None, str(e)

def extract_text_unified(file_bytes, file_type, page_idx):
    raw_text = ""
    if "image" in file_type: 
        raw_text, _ = api_call_direct("이 이미지 속의 텍스트를 모두 추출하세요. 줄바꿈 유지.", file_bytes)
        raw_text = raw_text or ""
    elif "pdf" in file_type:
        if PLUMBER_AVAILABLE:
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    st.session_state.total_pages = len(pdf.pages)
                    if page_idx < len(pdf.pages): raw_text = pdf.pages[page_idx].extract_text()
            except: pass
        if (not raw_text or len(raw_text) < 10) and FITZ_AVAILABLE:
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                st.session_state.total_pages = len(doc)
                if page_idx < len(doc): raw_text = doc[page_idx].get_text()
            except: pass
    return clean_raw_text(raw_text)

def get_page_image(file_bytes, file_type, page_idx):
    if "image" in file_type: return file_bytes
    if "pdf" in file_type and FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            if page_idx < len(doc): 
                pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                return pix.tobytes("png")
        except: pass
    return None

def generate_prompt_from_sheet(sheet_data):
    if not sheet_data: return ""
    rules = []
    for row in sheet_data[-150:]:
        orig, root, origin, pos, action = row.get('original_word',''), row.get('root_word',''), row.get('origin',''), row.get('pos',''), row.get('action','')
        if action == 'delete': rules.append(f"- '{orig}'는 분석 결과에서 제외.")
        elif action in ['modify', 'add']: rules.append(f"- '{orig}' 분석 결과는 원형:'{root}', 분류:'{origin}', 품사:'{pos}'가 정답.")
    return "\n[사용자 학습 데이터]:\n" + "\n".join(rules) + "\n"

def run_analysis_action(txt, img_bytes=None):
    if not txt.strip(): st.warning("분석할 내용이 없습니다."); return
    
    st.session_state.debug_log = f"[{datetime.now().strftime('%H:%M:%S')}] 분석 시작... 텍스트 길이: {len(txt)}\n"
    
    # [개선] 작업 진행 상태를 명시적으로 표시하여 연속 작업 충돌 방지
    with st.spinner("AI 분석 엔진 가동 중..."):
        s_data = get_sheet_data_fresh(st.session_state.mode_key)[1]
        
        prompt = f"""
        당신은 국어 형태소 분석 전문가입니다. 아래 지침에 따라 텍스트를 JSON 리스트로 정밀 분석하십시오.
        {generate_prompt_from_sheet(s_data)}
        
        [분석 핵심 규칙]
        1. **특수문자/조사/어미 제거**: 기호나 문장부호, 조사는 절대 결과에 포함하지 마십시오.
        2. **숫자 및 영어 제외**: 아라비아 숫자(0-9)나 영문자(A-Z, a-z)가 포함된 단어는 무조건 제외하십시오.
        3. **의존 명사**: '것', '수', '만큼' 등은 '명사' 품사로 분류하십시오.
        4. **동음이의어**: 문맥상 뜻이 갈리는 단어는 원형 뒤에 괄호로 뜻을 구분하십시오.
        5. **개체명 인식 (인명/지명)**: 성함은 원형 뒤에 '(이름)', 지역 명칭은 '(지명)'을 표기하십시오.
        
        [어종 판별]
        한자 기반 단어는 '한'으로, 순우리말은 '고', 서구 유래어는 '외'로 분류하십시오.
        
        [출력 양식: 반드시 한글 키 JSON 리스트]
        원본, 원형, 분류(고/한/외/혼), 품사(명사/동사/형용사/부사/관형사/대명사/감탄사)
        """
        
        raw, status = api_call_direct(prompt + f"\n[분석 대상]:\n{txt[:5000]}", img_bytes)
        # TypeError 방지를 위한 세션 상태 보호 로직
        st.session_state.last_raw_response = raw if raw else f"No response (Status: {status})"
        st.session_state.debug_log += f"API 상태: {status}\nAI 응답 길이: {len(raw) if raw else 0}\n"
        
        if not raw:
            st.error(f"AI 응답을 받지 못했습니다. 상태: {status}")
            return

        try:
            clean_json = raw.replace("```json", "").replace("```", "").strip()
            match = re.search(r'\[.*\]', clean_json, re.DOTALL)
            if not match:
                st.error("JSON 형식을 찾을 수 없습니다."); return
                
            res = json.loads(match.group())
            st.session_state.debug_log += f"추출된 단어 개수: {len(res)}\n"
            
            proc = []; temp_dict = {}
            om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
            pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사', '감탄사':'❗ 감탄사'}
            
            draft_items = []
            for r in res:
                o, root = str(r.get('원본') or '').strip(), str(r.get('원형') or '').strip()
                orig_v, pos_v = str(r.get('분류') or '혼').strip(), str(r.get('품사') or '명사').strip()
                
                if re.search(r'[0-9a-zA-Z]', o) or re.search(r'[0-9a-zA-Z]', root): continue
                if not o or not root: continue

                draft_items.append({'원본': o, '원형': root, '분류': orig_v, '품사': pos_v})
                key = (root, orig_v, pos_v)
                if key not in temp_dict: temp_dict[key] = []
                temp_dict[key].append(o)
            
            st.session_state.initial_draft = draft_items
            for (root, origin, pos), origs in temp_dict.items():
                cnts = Counter(origs)
                proc.append({
                    "삭제": False, "횟수": f"{sum(cnts.values())}회", 
                    "원본": ", ".join([f"{w}({c})" for w, c in cnts.items()]), 
                    "원형": root, "분류": om.get(origin, origin), "품사": pm.get(pos, pos)
                })
            
            st.session_state.analysis_result = proc
            st.session_state.debug_log += "데이터 병합 완료.\n"
            st.session_state.step = 3; st.rerun()
            
        except Exception as e:
            st.error(f"데이터 파싱 오류: {str(e)}")
            st.session_state.debug_log += f"오류 발생: {traceback.format_exc()}\n"

# =========================================================
# [5] UI: 메인 루프 (Wizard)
# =========================================================

with st.sidebar:
    st.markdown("### ⚙️ 시스템 설정")
    if st.session_state.debug_mode:
        if st.button("🐞 디버깅 모드 끄기"): st.session_state.debug_mode = False; st.rerun()
    else:
        st.markdown("<br>"*5, unsafe_allow_html=True)
        if st.button("🛠️ 관리자 모드 켜기"): st.session_state.debug_mode = True; st.rerun()

# STEP 0: 시작 화면
if st.session_state.step == 0:
    st.markdown("<h1 style='text-align: center; margin-bottom: 20px;'>📚 국어활동 AI 분석기</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #888; margin-bottom: 50px;'>원하는 언어 규범을 선택하여 분석을 시작하세요.</p>", unsafe_allow_html=True)
    
    _, c_south, c_north, _ = st.columns([1, 4, 4, 1])
    with c_south:
        if st.button("🏛️\n\n대한민국 표준어\n\n(표준국어대사전 기준)\n\n[ 시작하기 ]", key="btn_south", use_container_width=True):
            st.session_state.mode_key = "SOUTH"; st.session_state.step = 1; st.rerun()
    with c_north:
        if st.button("🏔️\n\n북한 문화어\n\n(문화어 규범 기준)\n\n[ 시작하기 ]", key="btn_north", use_container_width=True):
            st.session_state.mode_key = "NORTH"; st.session_state.step = 1; st.rerun()

# STEP 1: 데이터 소스 선택
elif st.session_state.step == 1:
    st.header("📂 데이터 소스 선택")
    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.subheader("📂 이어하기")
            up_excel = st.file_uploader("기존 분석 엑셀 파일 업로드", type=['xlsx'])
            if up_excel:
                with st.status("데이터 불러오는 중..."):
                    try:
                        st.session_state.master_df = pd.read_excel(up_excel)
                        st.session_state.step = 2; st.rerun()
                    except: st.error("엑셀 파일 형식이 올바르지 않습니다.")
    with col2:
        with st.container(border=True):
            st.subheader("🆕 새로 시작하기")
            if st.button("새 프로젝트 생성", use_container_width=True):
                st.session_state.master_df = None; st.session_state.step = 2; st.rerun()
    st.markdown("---")
    if st.button("⬅️ 모드 다시 선택"): st.session_state.step = 0; st.rerun()

# STEP 2: 자료 입력
elif st.session_state.step == 2:
    st.session_state.is_finished = False
    c_t, c_h = st.columns([8, 2])
    with c_t: st.header("📝 분석 자료 입력")
    with c_h:
        if st.button("🏠 처음으로"): st.session_state.clear(); st.rerun()

    # [수정] 시작 쪽수 설정을 최상단으로 이동하여 편의성 증대
    with st.expander("⚙️ 분석 환경 설정 (페이지 쪽수 설정)", expanded=True):
        st.session_state.start_offset = st.number_input("도서 1쪽의 실제 숫자 (시작 쪽수 설정)", value=st.session_state.start_offset)
        actual_p = st.session_state.page_idx + st.session_state.start_offset
        st.markdown(f"<div class='info-card'>💾 <b>저장 위치 안내:</b> 현재 작업 중인 페이지는 엑셀의 <b>'{actual_p}쪽'</b>으로 기록됩니다.</div>", unsafe_allow_html=True)

    input_method = st.radio("방식", ["📄 파일 분석", "✍️ 직접 입력"], horizontal=True, index=st.session_state.current_tab_idx, label_visibility="collapsed")

    if input_method == "📄 파일 분석":
        st.session_state.current_tab_idx = 0
        file = st.file_uploader("분석할 파일 업로드 (PDF, PNG, JPG)", type=['pdf', 'png', 'jpg'])
        
        current_fb = file.getvalue() if file else st.session_state.file_bytes
        current_ft = file.type if file else st.session_state.file_type
        
        if current_fb:
            if st.session_state.file_bytes != current_fb:
                with st.status("파일 읽는 중..."):
                    st.session_state.file_bytes = current_fb; st.session_state.file_type = current_ft
                    st.session_state.page_idx = 0
                    st.session_state.extracted_text = extract_text_unified(current_fb, current_ft, 0)
            
            c1, c2 = st.columns(2)
            with c1:
                # PDF/이미지 미리보기 (강화된 로직 적용)
                img = get_page_image(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx)
                if img: st.image(img, use_container_width=True)
                
                if st.session_state.file_type == "application/pdf":
                    st.markdown('<div class="control-card">', unsafe_allow_html=True)
                    st.markdown(f"<span class='status-badge'>📄 PDF 상태: {st.session_state.page_idx + 1} / {st.session_state.total_pages} 페이지</span>", unsafe_allow_html=True)
                    st.write("") 
                    j_col1, j_col2, j_col3 = st.columns([1, 1, 1])
                    with j_col1:
                        if st.button("◀ 이전 페이지", use_container_width=True, disabled=(st.session_state.page_idx <= 0)):
                            with st.spinner("페이지 이동 중..."):
                                st.session_state.page_idx -= 1
                                st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx); st.rerun()
                    with j_col2:
                        target_p = st.number_input("이동", min_value=1, max_value=st.session_state.total_pages, value=st.session_state.page_idx + 1, label_visibility="collapsed")
                        if target_p != st.session_state.page_idx + 1:
                            with st.spinner("페이지 점프 중..."):
                                st.session_state.page_idx = target_p - 1
                                st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx); st.rerun()
                    with j_col3:
                        if st.button("다음 페이지 ▶", use_container_width=True, disabled=(st.session_state.page_idx >= st.session_state.total_pages - 1)):
                            with st.spinner("페이지 이동 중..."):
                                st.session_state.page_idx += 1
                                st.session_state.extracted_text = extract_text_unified(st.session_state.file_bytes, st.session_state.file_type, st.session_state.page_idx); st.rerun()
                    st.markdown('</div>', unsafe_allow_html=True)
            
            with c2:
                txt_in = st.text_area("에디터 (추출 텍스트)", value=st.session_state.extracted_text, height=520)
                st.session_state.extracted_text = txt_in
                if st.button("🚀 분석 실행", type="primary", use_container_width=True): 
                    run_analysis_action(txt_in, st.session_state.file_bytes)
    else:
        st.session_state.current_tab_idx = 1
        direct_t = st.text_area("텍스트 입력 창", value=st.session_state.extracted_text, height=450)
        st.session_state.extracted_text = direct_t
        if st.button("🚀 분석 실행", type="primary", use_container_width=True): 
            run_analysis_action(direct_t)

# STEP 3: 결과 확인
elif st.session_state.step == 3:
    ch, cb = st.columns([8, 2])
    with ch: st.header("📊 분석 결과 확인")
    with cb:
        if st.button("⬅️ 입력 수정하기", use_container_width=True): st.session_state.step = 2; st.rerun()
    
    # [안정성 적용] 정밀 디버깅 모드 출력
    if st.session_state.debug_mode:
        with st.expander("🛠️ [정밀 디버깅] 분석 프로세스 로그", expanded=True):
            st.markdown(f"<div class='debug-box'>{st.session_state.get('debug_log', 'No log available')}</div>", unsafe_allow_html=True)
            # TypeError 방지를 위한 데이터 보호 처리
            raw_data = st.session_state.get('last_raw_response', '')
            if raw_data and isinstance(raw_data, str):
                st.code(raw_data, language="json", label="AI 응답 원본")
            else:
                st.info("AI 응답 데이터가 준비되지 않았습니다.")

    # [수정 반영] 분석 대상 원문 확인 영역 높이 대폭 확장 (가시성 개선)
    with st.expander("📝 분석 대상 원문 확인 (비교 대조용)", expanded=True):
        st.text_area("원문 원본", value=st.session_state.extracted_text, height=500, disabled=True)

    dlg_func = st.dialog if hasattr(st, "dialog") else st.experimental_dialog
    @dlg_func("➕ 단어 직접 추가")
    def add_manual():
        with st.form("manual_add_form"):
            o, r = st.text_input("원본 단어"), st.text_input("원형(기본형)")
            org = st.selectbox("어종 분류", ["고","한","외","혼"]); p = st.selectbox("품사", ["명사","동사","형용사","부사","관형사","대명사","고유명사","감탄사"])
            cnt = st.number_input("출연 횟수", 1, 100, 1)
            if st.form_submit_button("추가 완료"):
                with st.spinner("단어 추가 중..."):
                    om = {'고':'🔵 고', '한':'🟢 한', '외':'🔴 외', '혼':'🟣 혼'}
                    pm = {'명사':'📦 명사', '동사':'🏃 동사', '형용사':'🎨 형용사', '부사':'⚡ 부사', '관형사':'🔍 관형사', '대명사':'👤 대명사', '감탄사':'❗ 감탄사'}
                    st.session_state.analysis_result.append({"삭제": False, "횟수": f"{cnt}회", "원본": f"{o}(수동)", "원형": r, "분류": om.get(org, org), "품사": pm.get(p, p)})
                    st.rerun()

    # 데이터 에디터 출력 안정성 확보
    df_res = pd.DataFrame(st.session_state.analysis_result)
    if not df_res.empty:
        # 데이터 편집 시 로딩 느낌을 주기 위한 가이드
        st.info("💡 표의 내용을 직접 수정할 수 있습니다. 수정 후 '저장하기'를 눌러주세요.")
        edited = st.data_editor(
            df_res,
            column_config={
                "삭제": st.column_config.CheckboxColumn("삭제"),
                "원본": st.column_config.TextColumn("원본", disabled=True),
                "분류": st.column_config.SelectboxColumn("분류", options=["🔵 고", "🟢 한", "🔴 외", "🟣 혼"]),
                "품사": st.column_config.SelectboxColumn("품사", options=["📦 명사", "🏃 동사", "🎨 형용사", "⚡ 부사", "🔍 관형사", "👤 대명사", "고유명사", "❗ 감탄사"])
            },
            use_container_width=True, num_rows="dynamic", key="editor_grid"
        )
        if not edited.equals(df_res): st.session_state.analysis_result = edited.to_dict('records')
    else:
        st.warning("분석된 결과 단어가 없습니다. 숫자를 제외한 순수 한글 텍스트가 있는지 확인하세요.")

    if not st.session_state.is_finished:
        b1, b2, b3 = st.columns([1, 1, 2])
        with b1:
            if st.button("➕ 단어 추가", use_container_width=True): add_manual()
        with b2:
            if st.button("⛔ 선택 삭제", use_container_width=True):
                with st.spinner("삭제 반영 중..."):
                    st.session_state.analysis_result = [r for r in st.session_state.analysis_result if not r.get('삭제', False)]
                    st.rerun()
        with b3:
            if st.button("💾 저장하기 (완료)", type="primary", use_container_width=True):
                with st.status("데이터 통합 및 클라우드 저장 중..."):
                    save_logic_with_learning(); st.session_state.is_finished = True; st.balloons(); st.rerun()
    else:
        st.success("✅ 모든 분석 데이터가 성공적으로 통합되었습니다!")
        actual_p = st.session_state.page_idx + st.session_state.start_offset
        st.info(f"📍 현재 페이지 데이터는 마스터 엑셀의 **'{actual_p}쪽'**으로 성공적으로 합쳐졌습니다.")
        
        fname = f"국어활동_결과_{st.session_state.mode_key}_{datetime.now().strftime('%m%d_%H%M')}.xlsx"
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as w: 
            st.session_state.master_df.to_excel(w, index=False)
            
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(label=f"📥 {fname} 다운로드", data=buf.getvalue(), file_name=fname, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, type="primary")
        with c2:
            if st.button("🔄 다음 페이지 작업 (입력창 이동)", use_container_width=True):
                with st.spinner("세션 초기화 및 이동 중..."):
                    st.session_state.step = 2; st.session_state.is_finished = False; st.rerun()