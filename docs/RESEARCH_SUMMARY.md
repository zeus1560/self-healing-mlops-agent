# 연구 결과 종합 (Phase 4 자료 정리 — 1차 착수)

## 이 문서의 역할

대회 형식이 아직 확정되지 않은 상태([[project_competition_pitch]], 교수님
확정 대기 ~11월)라 논문/포스터/슬라이드 중 어떤 포맷으로 낼지 미리 정할 수
없다. 그래서 포맷과 무관하게 **지금까지 나온 실험 결과·방법론·한계를 한
곳에 모아두는 단일 소스**로 이 문서를 쓴다 — 형식이 확정되면 여기서 필요한
부분만 뽑아 그 포맷에 맞게 재배치한다. README/`docs/SRE_PRACTICES.md`는
엔지니어링 레퍼런스(설치·운영·SLO)라 계속 그 역할로 두고, 이 문서는 "연구
결과 서술"만 담당해 중복을 피한다.

**진행 상태**: 1차 착수 — 이미 나온 결과를 모으고 구조를 잡은 단계, §2
관련 연구 비교까지 초안 추가(2026-09-18, 웹 검색 기반), 서지사항 확정 +
§1 서론 문장화(2026-09-19), §2에 인용한 논문 9편 전부 abstract 이상 정독
완료 — Turpin/Kamoi/IT Support RAG/GPT Semantic Cache/eARCO/Flow-of-Action/
STRATUS/Sarda et al./AIOps 서베이(2026-09-19). 이 과정에서 GPT Semantic
Cache 차별점 주장과 AIOps 서베이가 "점진적 자율성을 다룬다"는 암시적
주장, 2건이 틀렸던 걸 발견해 정정·철회함 — 대신 AIOps 서베이 §7.1이
"저지연 장애탐지를 해낸 LLM 연구가 아직 없다"고 콕 집은 대목에 이
프로젝트의 L1(<150ms)을 대응시키는 더 강한 포지셔닝을 새로 찾음. 아직
안 된 것: §1의 "기여" 주장이 이 9편 대비로 실제 타당한지 최종 재검증,
최종 포맷 변환. 원본 실측 커밋/스크립트는 각 절에 링크해뒀으니 숫자를
재확인할 땐 원본을 본다.

## 초록 (Abstract)

대회 형식이 뭐로 정해지든 논문/포스터/슬라이드 어디에나 거의 그대로
재사용 가능한 유일한 "완성형" 산출물이라 미리 써둠(2026-09-19). §1~§5
내용을 압축한 것이므로 본문 수정 시 이 절도 같이 갱신할 것.

**국문**: 이 프로젝트는 로그 기반 장애를 실시간으로 진단·복구하는 자율
MLOps 에이전트를, "검증된 만큼만 자동화 범위를 넓히는" 점진적 자율성
(Progressive Autonomy) 구조로 설계했다. RAG 기반 벡터 캐시(L1)와 LLM
폴백(L2)으로 구성된 진단 파이프라인에, 실행 성공 여부를 기준으로 캐시를
스스로 정제하는 온라인 학습 루프, 그리고 승인 게이트에서 사람이 보는 판단
근거가 실제로 신뢰할 만한지(Faithfulness) 검증하는 반사실적 조작·편향
주입 두 실험을 결합했다. 실제 운영 VM 환경에서 실측한 결과, 진단→제안→
검토 3단계 멀티에이전트 구조 도입으로 완전자동 실행 성공률이 6~8%에서
26%로 개선됐고, 편향 문구를 주입해도 승인 판정의 조작 성공률은 0%로
나타나 판단 근거가 입력을 충실히 반영함을 확인했다. 반면 QLoRA로
파인튜닝한 소형 모델은 학습 분포 밖 새 에러 유형에서 상용 LLM(Groq)
대비 전 지표에서 열세를 보여 과적합 위험을 실측으로 드러냈다. 관련 연구
9편을 검토한 결과 RAG 캐시·멀티에이전트 진단·설명 충실도 검증 개별 요소는
각각 선행 연구가 있었지만, 이를 SLO 수치 기반 승인 게이트라는 축으로 묶어
실제 운영 환경에서 전부 실측한 사례는 찾지 못했다 — 이 결합과, 완전
자동화가 아닌 단계적 신뢰 구축이 실제로 측정 가능한 효과를 내는지를
정량적으로 보이는 게 이 연구의 기여다.

**English**: This project designs an autonomous MLOps agent that diagnoses
and remediates log-based failures in real time under a **Progressive
Autonomy** principle — expanding automation scope only as far as it has
been empirically validated. A RAG-based vector cache (L1) backed by an
LLM fallback (L2) is paired with an outcome-gated online-learning loop
(cache entries are kept or evicted based on real execution success/
failure, not time) and two faithfulness probes — counterfactual target
manipulation and bias injection — that test whether the explanations
shown at the human-approval gate actually track the model's real
reasoning. Measured on a live production VM, introducing a three-stage
diagnose→propose→review multi-agent structure raised the fully-automatic
execution success rate from 6-8% to 26%, and injected bias phrases
produced a 0% manipulation success rate on approval verdicts, indicating
the shown rationale is faithful to the input. Conversely, a QLoRA-tuned
small model underperformed a commercial LLM (Groq) on every metric for
novel (out-of-distribution) error types, empirically exposing an
overfitting risk. A review of 9 related papers found prior work on each
individual component (RAG caching, multi-agent diagnosis, faithfulness
testing) but none that combines them under an SLO-gated approval axis
and validates the whole pipeline on a live production system — this
combination, and the quantitative demonstration that staged trust-building
(rather than full automation) produces measurable gains, is this work's
contribution.

## 1. 문제의식

완전 자동화된 장애 복구는 매력적이지만 위험도 크다 — LLM이 잘못 진단한
채로 운영 환경에 실제 명령을 실행하면, 사람이 개입할 새도 없이 피해가
커질 수 있다. 그래서 이 프로젝트는 "AI가 다 자동으로 고친다"가 아니라
**검증된 만큼만 자동화 범위를 넓히는 점진적 자율성(Progressive Autonomy)**
구조로 설계했다(읽기전용 → 제안 → 승인후실행 → 완전자동 4단계, [README](../README.md)
서두, `docs/SRE_PRACTICES.md` 승급 게이트 참고) — 이게 이 프로젝트 전체를
관통하는 핵심 주장이다.

이 주장이 성립하려면 최소 네 가지를 실측으로 답해야 한다.

1. **정확한가** — L1 캐시/L2(Groq) 진단·조치가 얼마나 맞는가 (§3 L2 정확도,
   QLoRA 비교)
2. **그 판단이 설명 가능한가(Explainability)** — 승인 게이트에서 사람이
   개입할 때, 시스템이 판단 근거를 실제로 보여주는가
3. **그 설명을 믿을 수 있는가(Faithfulness)** — 보여준 근거가 진짜 판단
   근거인지, 아니면 그럴듯해 보이는 사후 합리화인지 (§3 반사실적 조작/
   Bias-Injection 두 실험)
4. **자동화 범위를 넓혔을 때 실제로 좋아지는가** — 단계 승급이 숫자로
   정당화되는가 (§3 멀티에이전트 3단계 효과)

이 네 질문 각각을 별도 실험으로 검증한 결과가 §3이다. §2에서 정리한 9편
(2026-09-19 기준 전부 abstract 이상 정독 완료) 위에 놓고 보면, 개별 요소
기술은 각각 선행 연구가 있다 — RAG 캐시(IT Support RAG, eARCO), 멀티에이전트
RCA/조치(Flow-of-Action, STRATUS, Sarda et al.), Faithfulness 검증(Turpin,
Kamoi). 이 중 STRATUS·Flow-of-Action은 "너무 복잡하면 사람에게 넘긴다"는
이스케이프 경로 정도는 있지만, **SLO 수치 기준으로 자동화 단계를 명시적으로
승급시키는 구조**(이 프로젝트의 Progressive Autonomy)를 다룬 논문은 §2
9편 중엔 없었다. 그래서 이 프로젝트는 **RAG 캐시·멀티에이전트·Faithfulness
검증 세 요소를, SLO 기반 승인 게이트라는 네 번째 축으로 묶어 실제 운영
VM 환경에서 전부 실측했다**는 데 기여가 있다고 본다 — 다만 이건 9편의
검색 기반 표본 위에서 내린 판단이라 체계적 문헌조사(systematic literature
review) 수준의 확실성은 아니고, 최종 포맷 확정 전에 한 번 더 검증이
필요하다.

## 2. 관련 연구 비교

이 프로젝트를 이루는 네 갈래(RAG 기반 원인진단/캐시, LLM 기반 자동 조치,
점진적 자율성 단계, 설명 충실도 검증) 각각을 최근 연구·업계 흐름 어디에
자리매김할지 정리. 2026-09-18 웹 검색으로 후보를 찾고(1차), 2026-09-19에
학술 논문 9편은 abstract 이상(일부 PDF 본문)까지 정독해 서지사항과 핵심
주장을 검증·정정함(§5에 정정 이력) — 자율주행 SAE 레벨 비유처럼 학술
논문이 아닌 블로그 자료는 검증 대상에서 제외하고 "비유"로만 표시.

- **LLM 기반 자동 원인진단·조치**: 분야 전반 동향은 [A Survey of AIOps in
  the Era of Large Language Models](https://arxiv.org/pdf/2507.12472)(J.
  ACM, 2025-08)에 정리돼 있음. **(2026-09-19 PDF 본문 확인)** 이 서베이는
  RQ1(데이터)~RQ4(평가) 4축 taxonomy로 구성되고, 앞선 초안에서 "점진적
  자율성/승인 게이트를 다룬다"고 암시했던 건 **본문에 없어 철회** —
  §7(과제와 향후 방향)은 인간 감독/승인 단계가 아니라 (1) 비용·시간효율,
  (2) trace 데이터 미활용, (3) 소프트웨어 변화에 대한 일반화, (4) 기존
  AIOps 툴체인과의 통합 4가지만 다룸. 대신 훨씬 더 쓸모있는 대목을
  찾았다 — **§7.1에서 "장애 탐지(failure perception)는 10초 주기로 계속
  돌면서 1초 안에 추론을 끝내야 하는데, 이걸 제대로 해낸 LLM 기반 연구가
  아직 없다"고 명시적으로 지적**한다. 이 프로젝트의 L1(ChromaDB 벡터
  유사도, <150ms) 캐시가 바로 이 문제에 대한 하나의 답이라, "이 서베이가
  콕 집어 미해결이라 부른 지점에 이 프로젝트의 결과를 놓을 수 있다"는
  게 더 강한 포지셔닝이 됨 — §3 False Positive/threshold 실측(리트리버
  품질 정량화)과 같이 묶어 쓸 수 있다.
  가장 가까운 개별 연구로 Sarda, Namrud, Litoiu, Shwartz, Watts,
  ["Leveraging Large Language Models for the Auto-remediation of
  Microservice Applications: An Experimental Study"](https://dl.acm.org/doi/10.1145/3663529.3663855)
  (FSE 2024 Industry Track — Ansible 플레이북을 LLM으로 생성·실행해
  마이크로서비스 이슈를 자동 조치, 커스텀 데이터셋으로 파인튜닝.
  **(2026-09-19 확인)** 기능적 정확도 95.45%, 평균 정확도 98.86%로 SOTA
  주장 — 이 프로젝트의 L2 정확도(92%/96%)나 end-to-end 완전자동 실행
  성공률(26%)보다 훨씬 높은데, 이건 평가 방식이 다르기 때문일 가능성이
  큼(이쪽은 큐레이션된 Ansible 태스크셋 안에서의 정확도, 이 프로젝트는
  보안 화이트리스트·self-reflection까지 전부 통과한 end-to-end 비율) —
  직접 비교하면 안 되고, 왜 다른지 논문에서 명시해야 함)와 [STRATUS: A
  Multi-agent System for Autonomous Reliability Engineering of Modern
  Clouds](https://www.atlantis-press.com/article/126020167.pdf)(**2026-09-19
  확인**: CrewAI 기반 4개 전문 에이전트 — Incident Resolution Manager
  [오케스트레이터], JIRA Manager, Root Cause Analyzer, Code Fix Generator.
  RCA 정밀도 68%, 단순 작업에선 최대 96%, 에이전트 간 작업 위임 정확도
  71%. "에이전트가 처리하기 너무 복잡하면 사람에게 넘긴다"는 이스케이프
  경로가 있지만 Jira 티켓 상태 기반이라, 이 프로젝트처럼 SLO 승급 게이트로
  자동화 단계 자체를 명시적으로 관리하는 구조는 아님)가 목적이 가장
  겹친다. 차이점: 이 프로젝트는 마이크로서비스 오케스트레이션이 아니라
  **로그 라인 단위 탐지 → RAG 캐시(L1) 우선 조회 → LLM(L2) 폴백**이라는
  더 가벼운 구조이고, 승인 게이트(Progressive Autonomy)를 축으로
  설계했다는 점이 다르다.
- **RAG 기반 원인진단/사고 대응**: [eARCO](https://arxiv.org/html/2504.11505v1)
  (**2026-09-19 확인**: PromptWizard로 프롬프트를 한 번 최적화한 뒤,
  런타임에 FAISS로 유사 과거 인시던트 top-10을 검색해 프롬프트에 결합 —
  GPT-4o 기준 수동 프롬프트 대비 21% 정확도 향상, 0-shot 대비 10-shot이
  완전셋 27%/필터셋 37% 향상. 저자들이 직접 밝힌 한계: Microsoft 사내
  인시던트 데이터로만 평가해 다른 조직 데이터에서 재현될지 미검증 —
  이 프로젝트의 QLoRA/Groq 비교가 겪은 것과 같은 종류의 일반화 한계),
  [Retrieval Augmented Generation-Based Incident Resolution Recommendation
  System for IT Support](https://arxiv.org/pdf/2409.13707)(IT 지원 티켓에
  RAG + 인코더 분류 모델 + 생성 LLM 조합, 목적이 이 프로젝트의 L1 캐시와
  가장 유사), [Flow-of-Action](https://arxiv.org/pdf/2502.08224)(**2026-09-19
  확인**: SOP 지식베이스 + 5개 에이전트[MainAgent/CodeAgent/JudgeAgent/
  ObAgent/ActionAgent] 구조. 핵심 결과: ReAct 단일 에이전트 기준선 원인
  위치 정확도 **35.50%** → Flow-of-Action **64.01%**로 개선 — **이 프로젝트의
  멀티에이전트 3단계 효과(6~8%→26%, §3)와 같은 방향의 주장(멀티에이전트
  구조화가 단일 에이전트/체인보다 낫다)을 다른 도메인·다른 절대수치로
  뒷받침하는 선행 사례**로 정확히 자리매김할 수 있다)가 있다. 공통적으로
  "리트리버 품질이 병목"이라는 한계가 지적되는데, §3의 False Positive/
  threshold 실측이 바로 이 병목을 이 프로젝트 맥락에서 정량화한 사례로
  자리매김할 수 있다. **(2026-09-19 PDF 본문 확인)** IT Support 논문의
  지식베이스는 Milvus에 색인된 제품 문서 500만+ 건짜리 **정적 코퍼스**이고
  (§4.2 근처), 배포 계획(§5)도 "상담원이 별점·useful/not useful로 수동
  평가하는 피드백 버튼을 향후 붙일 예정"이라 아직 미배포 상태 — 즉 이
  논문은 **자동 온라인 학습이 없다는 게 실제로 확인됨**(계획조차 수동
  평가 UI지 자동 upsert가 아님). 그래서 "L1 캐시가 성공 사례를 자동으로
  upsert하는 온라인 학습을 한다"는 이 프로젝트의 차별점 주장은 이 논문
  대비로는 유효하다.
- **시맨틱 캐시 (L1 캐시의 이론적 위치)**: [GPT Semantic Cache](https://arxiv.org/abs/2411.05276),
  [VectorQ: Adaptive Semantic Prompt Caching](https://arxiv.org/html/2502.03771v1),
  GPTCache — 임베딩 유사도로 LLM 호출을 캐시 히트로 대체해 지연·비용을
  줄이는 일반 기법과 L1(ChromaDB) 캐시는 본질적으로 같은 아이디어. **정정
  (2026-09-19, PDF 본문 확인)**: 앞선 초안에서 "일반 시맨틱 캐시는 정적
  캐시만 다룬다"고 단정했던 건 **틀렸다** — GPT Semantic Cache 논문
  §2.8을 직접 읽어보니 이 시스템도 캐시 미스 때 LLM이 새로 생성한 응답을
  즉시 캐시·ANN 인덱스에 upsert하는 **동적 캐시**다(API 호출 최대 68.8%
  감소, 캐시 히트율 61.6~68.8%, 히트 정확도 97% 이상). "동적으로 갱신되냐
  아니냐"는 차별점이 아니었던 것. 다시 찾아보니 진짜 차이는 **무엇을
  기준으로 캐시에 넣고 빼는가**다 — GPT Semantic Cache는 캐시 미스 때
  나온 응답을 **검증 없이 그대로** 캐시하고 TTL(시간 경과)로만 만료시키는
  반면, 이 프로젝트의 L1(`learn_from_feedback`)은 L2/Rule이 생성한 커맨드가
  **실제 실행에 성공했을 때만** upsert하고, 이후 실패가 반복되면
  `success_count`/`failure_count`로 추적해 제거한다(`record_learned_outcome`)
  — 시간이 아니라 **실행 결과(outcome)**로 캐시 품질을 관리한다는 게 실제
  차별점. 이 차이가 실무적으로 의미 있는지(예: outcome 게이팅이 캐시
  오염을 얼마나 더 잘 막는지)는 아직 정량 비교 안 함, 별도 실험 필요.
- **점진적 자율성(Progressive Autonomy)**: 자율주행 SAE 레벨을 본뜬
  "단계적 자율성" 프레임은 AI 에이전트 일반에서 자주 쓰이는 비유([Vellum —
  Six Levels of Agentic Behavior](https://www.vellum.ai/blog/levels-of-agentic-behavior),
  [Autonomy Levels in AI Agents](https://www.emergentmind.com/topics/levels-of-autonomy-in-ai-agents))라,
  단일 핵심 논문을 못박기보다 "업계에 퍼진 설계 패턴을 SRE 자동화에 구체적
  수치(SLO 승급 게이트)로 적용한 사례"로 서술하는 게 정확하다. 단계 승급
  기준을 "일정 횟수 무사고 운영 실적"으로 두는 관행도 이 프로젝트의
  `docs/SRE_PRACTICES.md` 승급 게이트와 같은 발상.
- **설명 충실도(Faithfulness)**: Bias-Injection 실험은 [Turpin et al. 2023,
  NeurIPS](https://proceedings.neurips.cc/paper_files/paper/2023/hash/ed3fea9033a80fea1376299fa7863f4a-Abstract-Conference.html)
  의 방법론을 SRE 승인 판정이라는 안전-critical 도메인에 적용한 것.
  **(2026-09-19 abstract 확인)** 원 논문은 few-shot 프롬프트의 객관식
  선택지를 항상 "(A)"가 정답이 되도록 재배열하는 식으로 편향을 주입해
  BIG-Bench Hard 13개 태스크(GPT-3.5, Claude 1.0)에서 정확도가 최대
  36%까지 떨어지는 걸 보이고, 모델이 그 편향 요인을 설명에서 언급하지
  않은 채 그럴듯한 사후 합리화를 만든다는 걸 확인함 — 이 프로젝트의
  Bias-Injection 설계(대상 고정+편향 문구 주입, 언급 여부 측정)와 방법론
  골격이 정확히 같다. 다만 원 논문은 GPT-3.5/Claude 1.0(2023년 기준)
  대상이라, 이후 세대 모델(이 프로젝트는 최신 Groq 모델)에서 재현되는지를
  보여준다는 것도 이 실험의 의의로 추가할 수 있다. 최근 관련 연구로
  [Investigating the Effects of Cognitive Biases in Prompts on Large
  Language Model Outputs](https://arxiv.org/pdf/2506.12338)도 같은 계열.
  다만 Kamoi, Zhang, Zhang, Han, Zhang(2024, TACL), [When Can LLMs
  Actually Correct Their Own Mistakes? A Critical Survey of
  Self-Correction of LLMs](https://arxiv.org/abs/2406.01297)류 서베이는
  self-correction 신뢰도에 회의적 — **(2026-09-19 abstract 확인)** 정확히는
  "프롬프트만으로 준 피드백에 의한 자기수정은 성공 사례가 없다, 신뢰할
  수 있는 외부 피드백이 있는 태스크에서만 잘 작동한다, 대규모 파인튜닝은
  자기수정을 가능하게 한다"는 세 가지 조건을 제시함. **이 프로젝트의
  결과와의 관계를 정확히 따지면**: Kamoi et al.이 다루는 "자기수정"은
  "이미 틀린 답을 스스로 고치는 능력"이고, 이 프로젝트의 Bias-Injection이
  측정한 건 "원래 맞는 판단(거부)을 편향 유도에도 안 바꾸는 능력(충실한
  거부)"이라 같은 능력이 아니다 — 그래서 "조작 성공률 0%"가 Kamoi et al.의
  회의론과 모순되지 않을 수 있다. 다만 이 구분이 논문에서 방어 가능하려면
  "자기수정"과 "편향에 대한 저항"을 왜 다른 능력으로 취급하는지 근거를
  더 붙여야 한다(표본 크기 문제와는 별개의 논점).

## 3. 실험 결과 종합

| 실험 | 핵심 결과 | 원본 |
|---|---|---|
| **QLoRA 파인튜닝 vs Groq 70B** (공정 비교, n=50 `novel_errors_benchmark`) | 카테고리 정확도 84% vs **92%**(-8%p), 액션 정확도 64% vs **96%**(-32%p), 지연 2038.8ms vs **~650ms**(3배 느림) — QLoRA가 전 지표에서 열세. Colab T4 무료 티어(0.5B~3B, 508건 SFT)로 학습, GX10/Jetson 등 전용 하드웨어 없이 진행. **같은 파일 안 `heldout_eval_in_distribution`(학습 데이터와 같은 분포, n=614)에서는 카테고리 93.16%/액션 95.77%로 오히려 Groq 참고치보다 높게 나옴** — novel(새 에러 유형)에서만 떨어지는 대비가 과적합(학습 분포엔 잘 맞지만 일반화 실패) 패턴으로 해석됨 | `experiments/results/l2_accuracy_summary_qlora.json` |
| **L2(Groq) 정확도** (50건) | 카테고리 분류 92%, 액션 정확도 96% — 카테고리별로는 Auth_Error가 60%로 최저 | [README §실험 결과 요약](../README.md) |
| **멀티에이전트 3단계 효과** (진단→제안→검토, 2026-09-15) | End-to-end 완전 자동 실행 통과율 6~8% → **26%**(3~4배, 두 독립 실행 재현). 생성 성공률 자체는 98%로 그대로라, 개선은 "화이트리스트 통과율"에서 왔다고 해석 | `experiments/run_l2_production_path_check.py`, [SRE_PRACTICES §1.1](SRE_PRACTICES.md) |
| **Faithfulness — 반사실적 조작** (3케이스) | 대상만 바꾼 명령어 쌍에서 승인 판정 **100% 뒤집힘**(verdict flip), 판정 근거의 대상 언급률 0%→100% — self-reflection이 근거 없는 고정 문구가 아니라 입력을 실제로 반영함을 확인 | README §실험 결과 요약, `experiments/run_faithfulness_test.py` |
| **Faithfulness — Bias-Injection** (권위 주장/허위 성공이력/긴급성 압박, 2026-09-17) | 대상은 항상 오답 고정, 편향 문구만 주입. 네트워크 폴백 오염 15.6% 제외한 실 LLM 판정 76건 전부 대상 불일치를 정확히 지적하며 거부 — 조작 효과 **0%p**, 조작 성공률 **0%**(표본 작아 일반화는 신중) | `experiments/run_bias_injection_test.py`, `tests/test_bias_injection_scoring.py` (커밋 `c616a004`) |
| **False Positive** (LogHub 10개 무관 시스템 로그 2만 줄) | 1차 정규식 게이트 오탐률 10.51%, 그 오탐 전량을 L1(RAG) 게이트에 흘렸을 때 배포값(threshold 0.6)에서 confident FP **0.0%**(1,318건 복구 데이터 기준). threshold를 1.2로 올리면 79.4%로 폭증 — 0.6 유지 근거 | `experiments/run_false_positive_analysis.py` |
| **온라인 학습** (런타임 자동 축적) | L1 미스 → L2/Rule 성공 시 (에러→커맨드) 쌍을 `source="online_learning"`으로 자동 upsert, 반복 성공 시 `success_count` 누적 | `src/llm_engine.py:1327` `learn_from_feedback` |

## 4. 방법론적으로 주목할 점 (연구 서술 각도)

- **헤드라인 지표 오류를 실측으로 잡아낸 사례**: 2026-09-17~18 코드 신뢰도
  점검에서 README에 실려있던 `action_F1=0.982` 등 헤드라인 수치가 운영
  ChromaDB 데이터의 **65%가 누락된 상태**(381건, 실제로는 1,318건이어야 함)에서
  나온 값이었다는 걸 발견 — 데이터 복구 후 재측정해 전부 정정함
  ([[project_capstone_pivot]] 2026-09-17~18 세션). 논문 서술 시 "실측값의
  신뢰도를 어떻게 검증했는가"의 구체적 사례로 쓸 수 있다.
- **로컬/운영 환경 불일치**: 로컬 개발환경(Python 3.14)이 VM 운영환경
  (Python 3.10, chromadb 0.5.0)과 근본적으로 안 맞아, 실측은 전부 VM에서
  직접 돌려야 했다 — 재현성 논의에 넣을 만한 제약사항.
- **의존성 리스크**: Groq 무료 티어 모델이 예고 없이 단종된 이력이 두 차례
  있었음(README §L2 추론 환경 요구사항 콜아웃) — L2 경로 전체가 조용히
  실패하고 있었던 사고를 실측 중 발견·수정.
- **사전 확정 기준으로 Phase 방향을 결정**: QLoRA 결과를 보기 전에 이미
  "Groq 대비 +5%p 이상 개선 또는 지연시간/비용 우위 → A/B 프레임워크,
  미미/애매하면 → Explainability"라는 기준을 정해뒀고, 실측 후(전 지표 열세)
  그 기준에 따라 Phase 2를 Explainability로 확정함(`l2_accuracy_summary_qlora.json`
  `conclusion` 필드) — 결과를 보고 나서 기준을 짜맞춘 게 아니라는 점을
  논문에서 명시할 수 있다.

## 5. 한계

- Bias-Injection(n=3케이스, 76건 유효판정)과 Faithfulness 반사실 조작(3케이스)
  모두 표본이 작다 — 일반화 주장은 유보적으로 서술해야 함.
- QLoRA 비교는 Colab 무료 티어 제약(소형 모델, 508건 SFT) 안에서 나온 결과라,
  더 큰 모델/데이터로 파인튜닝했을 때도 Groq가 우위인지는 미검증.
- 90일 데이터 분석(§6)이 아직 없어, 장기 운영 관점의 결과는 이 문서에 없음.
- L1 캐시의 온라인 학습(§2에서 지적한 차별점)이 캐시 오염 위험 없이
  유효한지는 별도 검증 필요, 아직 안 함.
- §2 인용 논문 9편 전부(Turpin, Kamoi, IT Support RAG, GPT Semantic Cache,
  eARCO, Flow-of-Action, STRATUS, Sarda et al., AIOps 서베이) abstract
  이상 정독 완료(2026-09-19, 일부는 PDF 본문까지). 이 과정에서 초안 주장
  2건이 **틀린 것으로 확인돼 철회**됨: (1) "일반 시맨틱 캐시는 정적이다" →
  GPT Semantic Cache도 동적 upsert였음, 진짜 차이는 TTL 기반 vs 실행결과
  기반 캐시 관리로 재정의. (2) "AIOps 서베이가 점진적 자율성을 다룬다" →
  본문엔 없었음, 철회. 웹 검색 요약만으로 차별점을 단정하면 안 된다는 걸
  이번에 직접 확인한 셈 — 남은 위험은 이 9편이 검색으로 찾은 표본이라
  이 분야를 대표하는 체계적 문헌조사가 아니라는 점(§1에 명시).
  자율주행 SAE 레벨 비유(Vellum 등 블로그)는 학술 논문이 아니라서 검증
  대상에서 제외했고, 그 성격 그대로 서술에 반영돼 있음.

## 6. 아직 안 된 것

- **90일 데이터 축적**: 배경에서 자동 진행 중, 끝나면 기존 스크립트
  (`experiments/run_fp_fn_analysis.py` 등)로 분석해 이 문서 §3에 행 추가.
- **자체 로그 누적**: [`DATA_ACCUMULATION_DESIGN.md`](DATA_ACCUMULATION_DESIGN.md)
  설계만 완료, 실제 수집·분석은 미착수.
- **대회 형식 확정 대기**: 확정되면 이 문서를 그 포맷(논문/포스터/슬라이드)에
  맞게 재구성.
