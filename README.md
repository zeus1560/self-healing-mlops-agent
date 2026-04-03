# 🚀 [Intel Arc & Iris Xe] AI 자율 운영 에이전트 프로젝트

이 프로젝트는 제한된 로컬 GPU 환경(Intel Arc / Iris Xe)에서 동작하는 '비용 0원'의 Self-Healing MLOps 에이전트를 구축하는 저장소입니다.

---

## 🛠️ 1. 초기 개발 환경 세팅

cd self-healing-mlops-agent # 해당 파일 경로로 가기

.\venv\Scripts\activate # 가상 환경 켜기

pip install -r requirements.txt # 새로 생긴 라이브러리들 추가 설치하기

🌿 2. Git 브랜치 작업 규칙 (매우 중요!)
우리 프로젝트는 안전을 위해 절대 main 브랜치에서 직접 작업하지 않습니다.

main: 최종 발표용 완성본만 올라가는 성역 (건드리지 않음)

dev: 개발 중인 코드들이 하나로 합쳐지는 테스트 공간

feature/기능이름: 👩‍💻 각자 코드를 짜는 개인 작업 공간 (여기서만 코딩합니다!)

🏃 3. 매일 작업하는 순서 (Daily Workflow)
Git이 처음이시라면 아래 5단계 순서만 그대로 따라 하시면 절대 코드가 날아가지 않습니다.

Step 1. 최신 코드 가져오기 (작업 시작 전)

git checkout dev # 일단 dev 브랜치로 이동
git fetch origin dev # dev 최신화
git pull origin dev # 팀원이 짠 최신 코드를 내 컴퓨터로 다운로드

Step 2. 내 작업 공간(브랜치) 만들기

# 예시: git checkout -b feature/data-pipeline

git checkout -b feature/본인이*만들*기능\_이름

Step 3. 열심히 코딩하기 💻
(VRAM 프로파일러, ETL 로그 수집기 등 각자 맡은 코드를 작성합니다.)

Step 4. 작업물 저장하고 깃허브에 올리기 (작업 끝난 후)

git add . # 변경된 파일 모두 선택
git commit -m "feat: ETL 파이프라인 초안 작성" # 메모 남기기
git push origin feature/본인이*만들*기능\_이름 # 깃허브에 작업한 브랜치 명을 입력 후 해당 브랜치에 업로드

Step 5. 합쳐달라고 요청하기 (Pull Request)

깃허브 웹사이트에 들어가면 초록색 Compare & pull request 버튼이 뜹니다.

클릭 후, 내 브랜치를 dev 브랜치로 합쳐달라고 요청(PR) 하면 팀장이 리뷰 후 합칩니다!
