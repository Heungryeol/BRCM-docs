# 작업 기록

## 2026-10-14 — v0.1.0 최초 버전

**배경:** hst-mcp 의 KB 빌더(`kb/build_kb.py`)는 HST 룰별로 KB 몇 건만 골라 받습니다.
KB 전체(VMware 범위), techdocs, 베스트 프랙티스를 별도로 모아 두고 hst-mcp 와 RAG(PAIS)에서 함께 쓰기 위해 분리했습니다.

**확인한 사실**
- knowledge.broadcom.com: sitemap index(하위 19개, 약 18.2만 건, 문서별 `lastmod` 있음), robots.txt 는 수집 허용·crawl-delay 없음. 문서 주소에 언어 표시는 없고 번역판은 slug 가 일본어 등 비 ASCII.
- KB 문서는 서버 렌더링 HTML. Products / Issue/Introduction / Environment / Cause / Resolution 구조 (hst-mcp 에서 71건 실측 파싱 성공).
- techdocs.broadcom.com: robots.txt 는 `User-agent: *` 허용·crawl-delay 10. 그러나 bastion 에서는 robots.txt 부터 HTTP 403 (사이트 방화벽). 우회하지 않고 `blocked` 처리 + 로컬 import 경로 제공.
- vmware.com/docs/<이름>: 백서·설계 가이드가 PDF 로 제공. sitemap 에는 없어 목록 지정 + GitHub 문서 안 링크 발견 방식으로 수집.
- vmware/vcf-security-and-compliance-guidelines: md 296 / pdf 97 / csv 118 (같은 이름 md·pdf 공존 → md 우선). regulatory-compliance 의 대용량 PDF 는 기본 제외.

**구현**
- `brcm_docs/http.py` robots.txt 준수·사이트별 간격·Retry-After·비 ASCII URL 인코딩, 403 사이트는 `Blocked`
- `brcm_docs/extract.py` HTML(제목 경로 보존)·KB 섹션·PDF·Markdown·CSV → 섹션
- `brcm_docs/store.py` SQLite(doc/section/fts/queue), 내용 해시로 변경 감지, lastmod 기반 재수집
- `brcm_docs/sources.py` kb / techdocs / github / urls / import
- `brcm_docs/cli.py` doctor · plan · crawl · import · status · search · export(sqlite/md/jsonl)
- `scripts/run-background.sh`, `scripts/systemd/*` (주간 증분)

**시험:** `tests/test_e2e.py` — 가짜 KB·techdocs·vmware.com/docs 서버와 로컬 git 저장소로 32개 항목 확인
(범위 판정·번역판 제외·404·재실행 시 재요청 없음·lastmod 변경 재수집·차단 사이트 무요청·import·export 3종).

**다음**
- [ ] bastion 에서 `plan` 으로 실제 규모 확인 → 전체 수집
- [ ] hst-mcp `build_kb.py` 가 이 코퍼스를 읽어 쓰도록 연결 (KB 재수집 없이 가이드 근거 선택)
- [ ] PAIS 데이터 소스에 `export -f md` 결과 적재 시험
