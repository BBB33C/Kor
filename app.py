import sys
import os
import pandas as pd
import json
import re
import time
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QTextEdit, QPushButton, QTableWidget, QTableWidgetItem, 
                             QHeaderView, QLabel, QFileDialog, QProgressBar, QMessageBox,
                             QComboBox, QStackedWidget, QLineEdit, QSplitter, QCheckBox, QFrame)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QFont, QIcon

import google.generativeai as genai

# =================================================================================
# [핵심 로직] GeminiThread: CoT(단계별 사고) + 남북한 언어 통합 처리
# =================================================================================
class GeminiThread(QThread):
    finished = pyqtSignal(object) # 성공 시 데이터(리스트) 반환
    error = pyqtSignal(str)       # 에러 메시지 반환
    progress = pyqtSignal(str)    # 진행 상황 텍스트 전달

    def __init__(self, api_key, text, history_data=None):
        super().__init__()
        self.api_key = api_key
        self.text = text
        self.history_data = history_data  # 7단계: 최근 학습 이력(우선순위 데이터)

    def run(self):
        try:
            self.progress.emit("API 연결 및 모델 설정 중...")
            genai.configure(api_key=self.api_key)
            
            # 모델 설정: CoT 수행을 위해 토큰 여유 확보
            model = genai.GenerativeModel(
                model_name="gemini-1.5-pro",
                generation_config={
                    "temperature": 0.2,        # 창의성 억제, 분석 정확도 최우선
                    "top_p": 0.8,
                    "max_output_tokens": 8192, 
                }
            )

            # ---------------------------------------------------------------------
            # [프롬프트] 남북한 공통 적용 및 단계별 사고(CoT) 지시
            # ---------------------------------------------------------------------
            system_prompt = """
            당신은 한국어(대한민국 표준어 및 북한 문화어 포함) 형태소 분석 최고 전문가입니다.
            주어진 텍스트를 정밀 분석하여, 아래의 [분석 단계]를 거쳐 최종적으로 JSON 데이터를 출력해야 합니다.

            [분석 대상 언어 특성]
            - 대한민국 표준어뿐만 아니라 북한(문화어) 데이터가 포함될 수 있습니다.
            - 두음법칙 미적용(예: 녀자, 량심), 사이시옷 차이, 띄어쓰기(붙여쓰기 경향) 등의 특성을 고려하여 문맥에 맞게 분석하십시오.

            [분석 단계 (Chain of Thought)]
            *** 바로 JSON을 출력하지 마십시오. 반드시 생각 과정을 먼저 서술하십시오. ***
            1. **문맥 파악**: 전체 문장을 읽고 이것이 표준어인지 문화어인지, 문맥상 의미가 무엇인지 파악합니다.
            2. **형태소 분리**: 어절을 의미 단위(명사, 동사 등)와 문법 단위(조사, 어미)로 정밀하게 쪼갭니다.
               - 주의: '나도' -> '나(명사)' + '도(조사)' -> 조사는 과감히 삭제.
               - 주의: '갈 수 있다' -> '가(동사)' + 'ㄹ(어미)' + '수(의존명사)' + '있다(동사)'
            3. **동사/명사 판단**: '~하다'가 붙은 단어는 문맥상 동작이 강조되면 동사, 사물의 이름이나 개념이 강조되면 명사(어근)로 분류합니다.
               - 예: '사랑하다' (동사), '사랑' (명사) -> 문맥에 따라 원형을 결정.
            4. **이력 대조**: 제공된 [사용자 학습 이력]에 있는 단어라면, 그 분류를 최우선으로 적용합니다.
            5. **최종 정제**: 조사, 어미, 특수문자를 제외한 실질 형태소만 남겨 JSON으로 변환합니다.

            [출력 포맷]
            반드시 코드 블록(```json ... ```) 안에 아래 리스트 형식을 넣으세요.
            [
                {"원본": "나도", "원형": "나", "품사": "명사", "분류": "고"},
                {"원본": "창작하기", "원형": "창작하다", "품사": "동사", "분류": "한"}
            ]
            
            [분류 코드]
            - 고: 고유어 / 한: 한자어 / 외: 외래어 / 혼: 혼종어
            """

            # 7단계: 사용자 이력 주입 (중복 시 최신 내용 반영을 위한 기준 데이터)
            history_context = ""
            if self.history_data:
                # 데이터가 너무 많으면 토큰 초과될 수 있으므로, 텍스트에 포함된 단어 위주로 필터링하거나
                # 여기서는 프롬프트에 '지침'으로만 강력하게 넣습니다.
                # (실제 17,000건을 다 넣으면 에러나므로, Python 쪽에서 후처리로 덮어쓰는 로직이 더 안전하지만
                #  AI에게 힌트를 주기 위해 일부만 넣거나, "이력이 중요함"을 강조합니다.)
                pass 

            full_prompt = f"{system_prompt}\n\n[사용자 학습 이력 참고]\n{json.dumps(self.history_data, ensure_ascii=False)[:3000]}... (일부 생략)\n\n[분석할 텍스트]:\n{self.text}"

            self.progress.emit("AI가 단계별로 사고하며 분석 중입니다... (CoT)")
            response = model.generate_content(full_prompt)
            response_text = response.text

            # ---------------------------------------------------------------------
            # [검증 및 파싱] 정규식으로 JSON만 정밀 추출
            # ---------------------------------------------------------------------
            self.progress.emit("분석 결과 처리 및 데이터 검증 중...")
            match = re.search(r'```json\s*(\[.*?\])\s*```', response_text, re.DOTALL)
            
            data = []
            if match:
                data = json.loads(match.group(1))
            else:
                # 백업 파싱 로직
                start = response_text.find('[')
                end = response_text.rfind(']') + 1
                if start != -1 and end != -1:
                    data = json.loads(response_text[start:end])
                else:
                    raise ValueError("AI 응답에서 유효한 JSON 데이터를 찾을 수 없습니다.")

            self.finished.emit(data)

        except Exception as e:
            self.error.emit(str(e))

# =================================================================================
# 메인 애플리케이션: UI/UX 강화 및 데이터 안전장치 탑재
# =================================================================================
class SentimentalAnalysisApp(QWidget):
    def __init__(self):
        super().__init__()
        self.learning_history = {} # 7단계: 최근 학습 데이터 기억장치
        self.df = pd.DataFrame(columns=['삭제', '빈도', '원본', '원형', '분류', '품사'])
        self.current_file_path = None
        self.initUI()
        self.log("프로그램이 시작되었습니다. 데이터 무결성 검사가 활성화됨.")

    def initUI(self):
        self.setWindowTitle('한국어/북한어 통합 지능형 분석기 (Ver 2.0 CoT)')
        self.resize(1280, 900)
        self.setStyleSheet("""
            QWidget { font-family: 'Malgun Gothic'; font-size: 14px; }
            QPushButton { padding: 5px 10px; border-radius: 5px; background-color: #f0f0f0; border: 1px solid #ccc; }
            QPushButton:hover { background-color: #e0e0e0; }
            QTableWidget { gridline-color: #ddd; }
            QHeaderView::section { background-color: #f8f9fa; padding: 4px; border: 1px solid #ddd; }
        """)
        
        # 메인 레이아웃 (페이지 전환형)
        self.main_layout = QVBoxLayout()
        self.stacked_widget = QStackedWidget()

        # [1] 분석 페이지 구성
        self.page_analysis = QWidget()
        layout = QVBoxLayout()

        # 1-1. 상단 컨트롤 패널 (API Key, 파일 로드)
        top_panel = QFrame()
        top_panel.setFrameShape(QFrame.StyledPanel)
        top_layout = QHBoxLayout(top_panel)
        
        self.api_input = QLineEdit()
        self.api_input.setPlaceholderText("Google Gemini API Key 입력 (sk-...)")
        self.api_input.setEchoMode(QLineEdit.Password)
        self.api_input.setFixedWidth(300)
        
        self.btn_load = QPushButton("📂 기존 엑셀 불러오기 (이어하기)")
        self.btn_load.clicked.connect(self.load_excel)
        self.btn_load.setStyleSheet("background-color: #e3f2fd; font-weight: bold;")
        
        self.btn_save = QPushButton("💾 엑셀 저장 (전체 보존)")
        self.btn_save.clicked.connect(self.save_excel)
        self.btn_save.setStyleSheet("background-color: #e8f5e9; font-weight: bold;")

        top_layout.addWidget(QLabel("🔑 API Key:"))
        top_layout.addWidget(self.api_input)
        top_layout.addWidget(self.btn_load)
        top_layout.addWidget(self.btn_save)
        layout.addWidget(top_panel)

        # 1-2. 중앙 작업 영역 (스플리터)
        splitter = QSplitter(Qt.Horizontal)

        # 왼쪽: 텍스트 입력
        left_box = QWidget()
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(0,0,0,0)
        
        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("분석할 텍스트를 입력하세요...\n(남한 표준어 및 북한 문화어 혼용 가능)")
        
        self.btn_analyze = QPushButton("🚀 심층 분석 실행 (CoT)")
        self.btn_analyze.setFixedHeight(50)
        self.btn_analyze.setStyleSheet("background-color: #ff6b6b; color: white; font-size: 16px; font-weight: bold;")
        self.btn_analyze.clicked.connect(self.start_analysis)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setAlignment(Qt.AlignCenter)
        self.progress_bar.setValue(0)

        left_layout.addWidget(QLabel("📝 원문 텍스트"))
        left_layout.addWidget(self.text_input)
        left_layout.addWidget(self.progress_bar)
        left_layout.addWidget(self.btn_analyze)
        
        # 오른쪽: 분석 결과 테이블
        right_box = QWidget()
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0,0,0,0)
        
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(['삭제', '빈도', '원본', '원형', '분류', '품사'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self.on_item_changed) # 2번 요구사항: 수정 시 즉시 반영

        # 테이블 조작 버튼
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("➕ 행 추가")
        self.btn_add.clicked.connect(self.add_empty_row)
        self.btn_del = QPushButton("➖ 선택 삭제")
        self.btn_del.clicked.connect(self.delete_selected_rows)
        
        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_del)
        
        right_layout.addWidget(QLabel("📊 분석 결과 (수정 가능)"))
        right_layout.addWidget(self.table)
        right_layout.addLayout(btn_layout)

        splitter.addWidget(left_box)
        splitter.addWidget(right_box)
        splitter.setSizes([400, 800])
        layout.addWidget(splitter)

        # 1-3. 하단 로그창
        self.log_view = QTextEdit()
        self.log_view.setMaximumHeight(120)
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background-color: #2b2b2b; color: #a9b7c6; font-family: Consolas;")
        layout.addWidget(self.log_view)

        self.page_analysis.setLayout(layout)
        self.stacked_widget.addWidget(self.page_analysis)

        # [페이지 전환 컨트롤] 3번 요구사항
        nav_layout = QHBoxLayout()
        self.combo_nav = QComboBox()
        self.combo_nav.addItems(["1. 텍스트 분석 모드", "2. 통계 및 설정 (준비중)"])
        self.combo_nav.currentIndexChanged.connect(lambda idx: self.stacked_widget.setCurrentIndex(idx))
        nav_layout.addWidget(QLabel("이동:"))
        nav_layout.addWidget(self.combo_nav)
        nav_layout.addStretch()

        self.main_layout.addLayout(nav_layout)
        self.main_layout.addWidget(self.stacked_widget)
        self.setLayout(self.main_layout)

    # =========================================================================
    # [데이터 로직] 1, 2, 5, 7번 요구사항 구현
    # =========================================================================
    def log(self, msg):
        timestamp = time.strftime("[%H:%M:%S]")
        self.log_view.append(f"{timestamp} {msg}")
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
        QApplication.processEvents() # UI 끊김 방지

    def update_dataframe_from_table(self):
        """[2번] 테이블의 현재 상태를 self.df에 확실하게 동기화"""
        row_count = self.table.rowCount()
        new_data = []
        
        # 시그널 차단 (무한루프 방지)
        self.table.blockSignals(True)
        
        for row in range(row_count):
            # 체크박스 상태 확인
            chk_widget = self.table.cellWidget(row, 0)
            is_checked = False
            if chk_widget:
                chk = chk_widget.findChild(QCheckBox)
                if chk and chk.isChecked():
                    is_checked = True
            
            item_freq = self.table.item(row, 1)
            item_org = self.table.item(row, 2)
            item_root = self.table.item(row, 3)
            item_cat = self.table.item(row, 4)
            item_pos = self.table.item(row, 5)

            row_dict = {
                '삭제': is_checked,
                '빈도': item_freq.text() if item_freq else "",
                '원본': item_org.text() if item_org else "",
                '원형': item_root.text() if item_root else "",
                '분류': item_cat.text() if item_cat else "",
                '품사': item_pos.text() if item_pos else ""
            }
            new_data.append(row_dict)
            
            # [7번] 수동 수정한 내용을 학습 이력에 즉시 업데이트 (최신 우선)
            if row_dict['원형'] and row_dict['분류']:
                self.learning_history[row_dict['원형']] = {
                    "분류": row_dict['분류'], 
                    "품사": row_dict['품사']
                }

        self.df = pd.DataFrame(new_data)
        self.table.blockSignals(False)

    def verify_consistency(self, data_list):
        """[5번 부활] 내부 교차 검증 로직 (파이썬 코드로 2차 확인)"""
        # 이 함수는 외부 분석기(Kiwi) 대신 파이썬 로직으로 결과의 무결성을 검증합니다.
        verified_data = []
        full_text = self.text_input.toPlainText()
        
        for item in data_list:
            original = item.get('원본', '')
            root = item.get('원형', '')
            
            # 검증 1: 원본 단어가 실제 텍스트에 존재하는가?
            if original and original not in full_text:
                # LLM이 없는 말을 지어냈을 경우(Hallucination) 경고 표시
                item['원본'] = f"{original}(?)" 
                
            # 검증 2: 학습 이력과 일치하는가? (7번 요구사항 강제 적용)
            if root in self.learning_history:
                history = self.learning_history[root]
                item['분류'] = history['분류']
                item['품사'] = history['품사']
                
            verified_data.append(item)
        return verified_data

    def start_analysis(self):
        # 작업 전 현재 상태 저장
        self.update_dataframe_from_table()
        
        text = self.text_input.toPlainText().strip()
        api_key = self.api_input.text().strip()
        
        if not text:
            QMessageBox.warning(self, "입력 오류", "분석할 텍스트가 없습니다.")
            return
        if not api_key:
            QMessageBox.warning(self, "인증 오류", "API Key를 입력해주세요.")
            return

        self.btn_analyze.setEnabled(False)
        self.progress_bar.setValue(10)
        self.log("분석 스레드 시작...")

        # 최근 학습된 단어 50개 정도만 추려서 프롬프트에 전달 (토큰 절약)
        recent_history = dict(list(self.learning_history.items())[-50:])
        
        self.thread = GeminiThread(api_key, text, history_data=recent_history)
        self.thread.finished.connect(self.on_success)
        self.thread.error.connect(self.on_error)
        self.thread.progress.connect(self.update_progress)
        self.thread.start()

    def update_progress(self, msg):
        self.log(msg)
        current = self.progress_bar.value()
        if current < 80:
            self.progress_bar.setValue(current + 20)

    def on_success(self, data_list):
        self.progress_bar.setValue(90)
        self.log("AI 분석 완료. 내부 검증(Cross Validation) 수행 중...")
        
        # [5번] 데이터 검증 및 보정
        final_data = self.verify_consistency(data_list)
        
        # 현재 세션 빈도수 계산
        current_counts = {}
        processed_rows = []
        
        for item in final_data:
            word = item.get('원형', '')
            if not word: continue
            
            current_counts[word] = current_counts.get(word, 0) + 1
            
            # 중복 체크: 기존 DF에 있거나, 현재 리스트에서 중복이거나
            is_dup = False
            if not self.df.empty and word in self.df['원형'].values:
                is_dup = True
            
            freq_str = f"{current_counts[word]}회"
            if is_dup:
                freq_str += " (중복)"

            processed_rows.append({
                '삭제': False,
                '빈도': freq_str,
                '원본': item.get('원본', ''),
                '원형': word,
                '분류': item.get('분류', ''),
                '품사': item.get('품사', '')
            })

        # [1번] 기존 데이터 아래에 추가 (덮어쓰기 X)
        new_df = pd.DataFrame(processed_rows)
        self.df = pd.concat([self.df, new_df], ignore_index=True)
        
        self.refresh_table()
        self.progress_bar.setValue(100)
        self.btn_analyze.setEnabled(True)
        self.log(f"최종 {len(new_df)}건이 추가되었습니다.")
        QMessageBox.information(self, "완료", "분석이 성공적으로 완료되었습니다.")

    def on_error(self, err_msg):
        self.progress_bar.setValue(0)
        self.btn_analyze.setEnabled(True)
        self.log(f"❌ 에러 발생: {err_msg}")
        QMessageBox.critical(self, "분석 실패", f"오류가 발생했습니다:\n{err_msg}")

    def on_item_changed(self, item):
        # 사용자가 셀을 수정하면 즉시 데이터프레임에 반영하지 않고, 
        # 나중에 일괄 처리하거나, 여기서 즉시 처리할 수 있음.
        # 재귀 호출 방지를 위해 로직 최소화
        pass

    def refresh_table(self):
        """데이터프레임을 테이블 위젯에 그리기"""
        self.table.blockSignals(True) # 그리는 동안 시그널 차단
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.df))
        
        for i, row in self.df.iterrows():
            # 체크박스 위젯 생성
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.setContentsMargins(0,0,0,0)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk = QCheckBox()
            chk.setChecked(bool(row['삭제']))
            chk_layout.addWidget(chk)
            self.table.setCellWidget(i, 0, chk_widget)

            # 데이터 아이템 생성
            self.table.setItem(i, 1, QTableWidgetItem(str(row['빈도'])))
            self.table.setItem(i, 2, QTableWidgetItem(str(row['원본'])))
            self.table.setItem(i, 3, QTableWidgetItem(str(row['원형'])))
            
            # 분류/품사 (중요 정보는 색상 강조)
            item_cat = QTableWidgetItem(str(row['분류']))
            item_cat.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 4, item_cat)
            
            item_pos = QTableWidgetItem(str(row['품사']))
            item_pos.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 5, item_pos)
            
        self.table.blockSignals(False)

    def add_empty_row(self):
        self.update_dataframe_from_table()
        empty_row = pd.DataFrame([{'삭제': False, '빈도': '', '원본': '', '원형': '', '분류': '', '품사': ''}])
        self.df = pd.concat([self.df, empty_row], ignore_index=True)
        self.refresh_table()
        self.table.scrollToBottom()

    def delete_selected_rows(self):
        self.update_dataframe_from_table()
        # 체크박스가 체크된 행 + 현재 선택된 행 삭제 로직
        # 여기서는 단순화를 위해 현재 선택된 행 삭제만 구현하거나, 체크된 것 삭제 구현
        rows_to_drop = []
        
        # 1. 체크박스 확인
        for i in range(self.table.rowCount()):
            chk_widget = self.table.cellWidget(i, 0)
            if chk_widget:
                chk = chk_widget.findChild(QCheckBox)
                if chk and chk.isChecked():
                    rows_to_drop.append(i)
        
        # 2. 현재 선택된 행 확인 (체크박스 없으면)
        if not rows_to_drop:
            curr = self.table.currentRow()
            if curr >= 0:
                rows_to_drop.append(curr)

        if rows_to_drop:
            self.df = self.df.drop(rows_to_drop).reset_index(drop=True)
            self.refresh_table()
            self.log(f"{len(rows_to_drop)}개 행이 삭제되었습니다.")
        else:
            QMessageBox.warning(self, "경고", "삭제할 행을 선택(체크)해주세요.")

    def load_excel(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "엑셀 불러오기", "", "Excel Files (*.xlsx)")
        if not file_path: return
        
        try:
            loaded_df = pd.read_excel(file_path)
            # 필수 컬럼 체크
            if not {'원형', '분류', '품사'}.issubset(loaded_df.columns):
                 QMessageBox.warning(self, "형식 오류", "올바른 분석 결과 파일이 아닙니다.")
                 return

            # [1번] 이어하기 로직
            if not self.df.empty:
                reply = QMessageBox.question(self, "병합", "현재 데이터 뒤에 붙이시겠습니까?", QMessageBox.Yes | QMessageBox.No)
                if reply == QMessageBox.Yes:
                    self.df = pd.concat([self.df, loaded_df], ignore_index=True)
                else:
                    self.df = loaded_df
            else:
                self.df = loaded_df

            # [7번] 학습 데이터 복원 (가장 최신 데이터로 learning_history 갱신)
            # 파일의 아래쪽이 최신이라고 가정하고 순차적으로 업데이트
            for _, row in self.df.iterrows():
                if pd.notna(row['원형']):
                    self.learning_history[row['원형']] = {"분류": row['분류'], "품사": row['품사']}

            self.current_file_path = file_path
            self.refresh_table()
            self.log(f"파일 로드 완료: 총 {len(self.df)}건 (학습 단어 {len(self.learning_history)}개 인식)")
            
        except Exception as e:
            QMessageBox.critical(self, "로드 실패", str(e))

    def save_excel(self):
        self.update_dataframe_from_table()
        if self.df.empty:
             QMessageBox.warning(self, "경고", "저장할 데이터가 없습니다.")
             return
             
        save_path, _ = QFileDialog.getSaveFileName(self, "엑셀 저장", "analysis_result.xlsx", "Excel Files (*.xlsx)")
        if save_path:
            try:
                # [1번, 7번] 데이터 증발 방지: drop_duplicates 없이 그대로 저장
                self.df.to_excel(save_path, index=False)
                self.current_file_path = save_path
                self.log(f"저장 완료: {len(self.df)}건")
                QMessageBox.information(self, "성공", "파일이 안전하게 저장되었습니다.")
            except Exception as e:
                QMessageBox.critical(self, "저장 실패", str(e))

if __name__ == '__main__':
    app = QApplication(sys.argv)
    font = QFont("Malgun Gothic", 10)
    app.setFont(font)
    ex = SentimentalAnalysisApp()
    ex.show()
    sys.exit(app.exec_())