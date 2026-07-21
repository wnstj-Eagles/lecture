import os
import re
import time
import fitz  # PyMuPDF
from typing import List, Optional
from pydantic import BaseModel, Field
from openai import OpenAI, RateLimitError
from dotenv import load_dotenv
import pandas as pd

MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 15

# ==========================================
# 1. 환경 변수 로드 및 API 클라이언트 초기화
# ==========================================
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError(".env 파일에서 OPENAI_API_KEY를 찾을 수 없습니다.")

client = OpenAI(api_key=api_key)

# ==========================================
# 1-1. 차주 매핑 엑셀 로드 (임시관리번호 -> 차주명/금고명)
#    - "파일차주명.xlsx"의 "차주" 시트, 2번째 행이 헤더
#    - PDF 파일명 맨 앞의 임시관리번호(예: A001)로 조회
#    - 금고명: 을구 근저당권 필터링 기준(근저당권자는 통상 대출기관인 금고 명의로 기재됨)
#    - 고객명(차주명): 참고용 채무자/소유자 정보
# ==========================================
BORROWER_MAPPING_PATH = os.path.join("data", "파일차주명.xlsx")
BORROWER_MAPPING_SHEET = "차주"
BORROWER_CODE_COLUMN = "임시관리번호"
BORROWER_NAME_COLUMN = "고객명"
INSTITUTION_NAME_COLUMN = "금고명"
BORROWER_CODE_PATTERN = re.compile(r"^([A-Za-z]\d{3})")


def load_borrower_mapping(path: str) -> dict:
    df = pd.read_excel(path, sheet_name=BORROWER_MAPPING_SHEET, header=1)
    df = df[[BORROWER_CODE_COLUMN, BORROWER_NAME_COLUMN, INSTITUTION_NAME_COLUMN]].dropna(
        subset=[BORROWER_CODE_COLUMN, BORROWER_NAME_COLUMN]
    )

    mapping = {}
    for code, name, institution in zip(
        df[BORROWER_CODE_COLUMN], df[BORROWER_NAME_COLUMN], df[INSTITUTION_NAME_COLUMN]
    ):
        code = str(code).strip().upper()
        if not BORROWER_CODE_PATTERN.fullmatch(code):
            continue  # 시트 하단의 "END" 등 마커 행 제외
        mapping[code] = {
            "borrower_name": str(name).strip(),
            "institution_name": str(institution).strip() if pd.notna(institution) else None,
        }
    return mapping


def extract_borrower_code(filename: str) -> Optional[str]:
    match = BORROWER_CODE_PATTERN.match(filename.strip())
    return match.group(1).upper() if match else None


# ==========================================
# 2. 구조화할 데이터 스키마 정의 (Pydantic Field 통제)
# ==========================================
class RegistryRow(BaseModel):
    address: str = Field(description="표제부 지번주소. 대괄호 [ ] 포함 그대로 기재.")
    building_type: str = Field(description="건물내역. 토지인 경우 반드시 '토지' 또는 지목 기재.")
    building_area: str = Field(description="표제부 건물면적 합계. 단위를 완벽히 제외하고 숫자만 기재. 토지인 경우 '미확인' 기재.")
    land_area: str = Field(description="표제부 토지면적 합계. 단위 제외. 건물이 아닌 '토지' 지번인 경우 이곳에 면적 기재. 집합건물은 대지권비율을 적용한 합계 기재.")
    owner: str = Field(description="갑구의 소유자 성명 또는 법인명. 주소나 주민번호는 절대 포함하지 말고 '이름'만 단답형으로 기재.")
    mortgage_date: str = Field(description="최초 근저당권설정일 (YYYY-MM-DD)")
    mortgage_amount: str = Field(description="대상 채권자의 근저당권설정액 합계. 단위를 제외한 순수 숫자.")
    prior_rights: str = Field(description="대상 채권자보다 선순위인 가압류, 임차권, 전세권 등. 없으면 '기록사항 없음'")
    auction_case_number: str = Field(description="경매사건번호")
    auction_court: str = Field(description="경매법원")
    auction_received_date: str = Field(description="경매접수일자")

class NPLRegistryExtraction(BaseModel):
    rows: List[RegistryRow] = Field(description=(
        "이 문서 자체의 표제부에 면적 등 실제 정보가 기재되어 있는 '주된 물건'(지번)만 각각 "
        "별개의 행(Row)으로 분리하여 배열에 담으십시오. "
        "을구의 공동담보목록이나 대지권의 목적인 토지의 표시에 다른 지번이 단순히 참조/나열만 "
        "되어 있고 이 문서 안에 그 지번 자체의 표제부(면적 등) 정보가 없다면, 그 지번은 "
        "별도의 행으로 만들지 말고 무시하십시오. "
        "단, 집합건물처럼 한 문서 안에 여러 개의 독립된 전유부분(호실)이 각각 고유한 표제부/면적 "
        "정보와 함께 완전하게 기재되어 있다면 그 경우에는 각각을 별개의 행으로 분리하십시오."
    ))

# ==========================================
# 3. PDF에서 순수 텍스트만 긁어오는 함수 (Vision 대체)
# ==========================================
def extract_text_from_pdf(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    full_text = ""
    # 문서 전체 페이지의 텍스트를 그대로 복사하여 하나의 문자열로 결합
    for page in doc:
        full_text += page.get_text("text") + "\n\n"
    doc.close()
    return full_text

# ==========================================
# 4. 메인 파이프라인 프로세스
# ==========================================
def main():
    input_folder = "data"
    output_excel_path = "data/result.xlsx"
    
    if not os.path.exists(input_folder):
        return
        
    pdf_files = sorted([f for f in os.listdir(input_folder) if f.lower().endswith(".pdf")])
    if not pdf_files:
        print("PDF 파일이 없습니다.")
        return

    try:
        borrower_mapping = load_borrower_mapping(BORROWER_MAPPING_PATH)
        print(f"차주 매핑 로드 완료: {len(borrower_mapping)}건")
    except Exception as e:
        print(f"경고: 차주 매핑 엑셀 로드 실패 ({e}). 대상 채권자 필터링 없이 진행합니다.")
        borrower_mapping = {}

    all_extracted_rows = []

    for index, filename in enumerate(pdf_files, start=1):
        doc_id = f"D{index:03d}"
        pdf_path = os.path.join(input_folder, filename)

        borrower_code = extract_borrower_code(filename)
        borrower_info = borrower_mapping.get(borrower_code) if borrower_code else None
        borrower_name = borrower_info["borrower_name"] if borrower_info else None
        institution_name = borrower_info["institution_name"] if borrower_info else None
        if borrower_code and not borrower_info:
            print(f"[{doc_id}] 경고: 임시관리번호 '{borrower_code}'를 매핑 엑셀에서 찾지 못했습니다. 필터링 없이 전체 근저당권을 추출합니다.")
        elif not borrower_code:
            print(f"[{doc_id}] 경고: 파일명에서 임시관리번호를 추출하지 못했습니다 ('{filename}'). 필터링 없이 전체 근저당권을 추출합니다.")
        elif not institution_name:
            print(f"[{doc_id}] 경고: 임시관리번호 '{borrower_code}'의 금고명이 매핑 엑셀에 비어있습니다. 필터링 없이 전체 근저당권을 추출합니다.")

        print(f"\n[{doc_id}] {filename} 텍스트 추출 및 분석 시작... (대상 금고: {institution_name or '미지정'}, 차주: {borrower_name or '미지정'})")

        try:
            # 이미지 렌더링 대신 순수 텍스트 추출 (수십 배 빠름)
            pdf_raw_text = extract_text_from_pdf(pdf_path)
        except Exception as e:
            print(f"[{doc_id}] 텍스트 추출 실패: {e}")
            continue
            
        # ==========================================
        # 5. 프롬프트 페이로드 (텍스트 다이렉트 주입)
        # ==========================================
        if institution_name:
            target_creditor_line = (
                f"(선순위 파악 및 근저당권 필터링 기준 기관: '{institution_name}')\n"
                f"을구에 여러 근저당권자가 존재하더라도, mortgage_date/mortgage_amount는 "
                f"근저당권자가 '{institution_name}'(완전히 동일한 표기가 아니어도 동일 금융기관으로 "
                f"판단되면 포함)인 근저당권만 집계하십시오. 그 외 근저당권자는 mortgage_date/"
                f"mortgage_amount에 포함하지 말고, '{institution_name}' 명의 근저당권보다 등기 순위가 "
                f"앞서는 경우에만 prior_rights에 요약해 기재하십시오."
            )
            if borrower_name:
                target_creditor_line += f"\n참고로 이 물건의 채무자(차주)는 '{borrower_name}'입니다."
        else:
            target_creditor_line = (
                "(선순위 파악 기준 기관이 지정되지 않았습니다. 을구의 모든 근저당권을 "
                "mortgage_date/mortgage_amount에 그대로 나열하십시오.)"
            )

        content_payload = [
            {
                "type": "text",
                "text": (
                    "당신은 NPL 실사 전문 데이터 파서(Parser)입니다.\n"
                    "아래 제공된 NPL 등기부등본의 '원본 텍스트'를 분석하여 JSON 데이터를 추출하십시오.\n"
                    "가장 중요한 임무는 이 문서 자체의 표제부에 실제 정보(면적 등)가 기재된 '주된 물건' "
                    "지번을 단 하나도 누락하지 말고 각각 독립된 데이터 행(Row)으로 분리해 내는 것입니다. "
                    "단, 을구의 공동담보목록이나 대지권의 목적인 토지의 표시처럼 다른 지번이 참조/나열만 "
                    "되어 있고 그 지번 자체의 표제부 정보가 이 문서에 없다면 행으로 만들지 마십시오.\n"
                    "각 필드에 대한 구체적인 작성 규칙은 JSON 스키마의 설명(description)을 100% 엄수하십시오.\n"
                    f"{target_creditor_line}\n\n"
                    "====================================\n"
                    f"[등기부등본 원본 텍스트 데이터]\n{pdf_raw_text}\n"
                    "====================================\n"
                )
            }
        ]
        
        response = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = client.beta.chat.completions.parse(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": content_payload}],
                    response_format=NPLRegistryExtraction,
                )
                break
            except RateLimitError as e:
                if attempt == MAX_RETRIES:
                    print(f"[{doc_id}] rate limit 재시도 {MAX_RETRIES}회 초과, 이 문서는 건너뜁니다: {e}")
                    break
                wait_seconds = RETRY_BACKOFF_SECONDS * attempt
                print(f"[{doc_id}] rate limit 도달, {wait_seconds}초 후 재시도 ({attempt}/{MAX_RETRIES})...")
                time.sleep(wait_seconds)
            except Exception as e:
                print(f"[{doc_id}] API 분석 오류: {e}")
                break

        if response is None:
            continue

        try:
            extracted_data = response.choices[0].message.parsed

            for row in extracted_data.rows:
                row_dict = row.model_dump()
                final_row = {
                    "doc_id": doc_id,
                    "file_name": filename,
                    "borrower_code": borrower_code,
                    "borrower_name": borrower_name,
                    "institution_name": institution_name,
                    **row_dict,
                }
                all_extracted_rows.append(final_row)
                
            print(f"[{doc_id}] 분석 완료 및 적재 성공.")
            
        except Exception as e:
            print(f"[{doc_id}] API 분석 오류: {e}")
            
    if all_extracted_rows:
        df = pd.DataFrame(all_extracted_rows)
        df.to_excel(output_excel_path, index=False)
        print(f"\n최종 완료: 통합 데이터가 {output_excel_path}에 저장되었습니다.")
        print(df)
    else:
        print("\n추출된 데이터가 존재하지 않습니다.")

if __name__ == "__main__":
    main()