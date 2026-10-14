# brcm-docs

Broadcom VMware 문서 수집기 — **Broadcom KB 전체(VMware 범위) · techdocs · 공식 베스트 프랙티스**를 한 곳(SQLite + 전문검색)에 모으고,
에어갭 환경으로 옮길 수 있는 묶음(sqlite / markdown / jsonl)으로 내보냅니다.

- **hst-mcp**: 조치 가이드의 근거(KB·공식 문서)로 사용
- **PAIS 등 RAG**: `export -f md` 결과를 데이터 소스로 바로 업로드

수집은 인터넷이 되는 빌드 머신(bastion)에서 하고, 결과 묶음만 내부 서버로 옮깁니다.

## 수집 출처

| 출처 | 대상 | 방식 | 규모·시간 (요청 간격 2초 기준) |
|---|---|---|---|
| `github` | Broadcom 공식 [vcf-security-and-compliance-guidelines](https://github.com/vmware/vcf-security-and-compliance-guidelines) — SCG 컨트롤 CSV, 하드닝·보안 설계·랜섬웨어 방어 가이드, 보안 권고 FAQ | git clone → md/pdf/csv | 수백 건 · 수 분 |
| `urls` | `vmware.com/docs/*` 백서·설계 가이드 (Performance Best Practices, vSAN Design Guide 등) + GitHub 문서 안에서 발견한 링크 | PDF/HTML | 수십 건 · 수 분 |
| `kb` | knowledge.broadcom.com 전체 sitemap (약 18만 건) 중 VMware 제품 문서 | sitemap → 문서별 | VMware 문서 우선 수집, 전체 판정까지 수일 (중단·재개 가능) |
| `techdocs` | techdocs.broadcom.com 의 VCF 9.x 문서 트리 | sitemap → 페이지 | robots.txt crawl-delay 10초 준수 |

### KB 범위 판정 (`kb.scope: vmware`)
1. 번역판(주소가 영문이 아닌 문서)은 제외 — 영문 원문만
2. 주소(slug)에 VMware 단어(esxi, vcenter, vsan, nsx …)가 있으면 **먼저** 수집
3. slug 가 확실히 다른 제품(symantec, mainframe, db2 …)이면 받지 않음 (`--no-slug-skip` 으로 끔)
4. 나머지는 받아서 **Products 필드**로 판정 — VMware 가 아니면 제목·제품만 기록하고 본문은 저장하지 않음

`scope: all` 이면 전부 저장합니다.

### 자동 수집이 막힌 사이트
수집기는 자신을 정직하게 밝히는 User-Agent 를 쓰고 robots.txt 를 지킵니다.
사이트 방화벽(CDN)이 자동 수집을 403 으로 막으면 **우회하지 않고** 그 출처를 `blocked` 로 기록합니다
(현재 bastion 에서 techdocs 가 이 경우). 이때는 브라우저로 받은 PDF/HTML 을 넣습니다.

```bash
./brcm-docs import ~/techdocs-pdf --as-source techdocs-local
```

## 설치 (Ubuntu 22.04 bastion)

```bash
git clone git@github.com:Heungryeol/BRCM-docs.git ~/brcm-docs && cd ~/brcm-docs
apt-get install -y python3-yaml python3-pypdf2 git
./brcm-docs doctor          # 사이트 접근·robots.txt·의존성 점검
```

Python 3.10 이상, 외부 패키지는 PyYAML 과 (PDF 용) pypdf/PyPDF2 뿐입니다.

## 사용

```bash
./brcm-docs plan                         # sitemap·저장소를 읽어 대기열만 채우고 규모·예상 시간 표시
scripts/run-background.sh                # 전체 수집을 백그라운드로 (SSH 끊겨도 계속)
tail -f data/logs/latest.log             # 진행 상황
./brcm-docs status                       # 출처별 대기·완료·범위 밖·오류 건수

./brcm-docs crawl -s github,urls         # 베스트 프랙티스만 (수 분)
./brcm-docs crawl -s kb --hours 8        # KB 를 8시간만 돌고 멈춤 → 다음에 이어서
./brcm-docs crawl --retry-errors         # 오류 항목 다시 시도
./brcm-docs search "lockdown mode"       # 검색 시험
```

- **이어하기:** 중지(Ctrl+C 또는 `kill $(cat data/crawl.pid)`)하면 현재 문서까지 저장하고 멈춥니다. 같은 명령을 다시 실행하면 이어서 합니다.
- **증분 수집:** sitemap 의 `lastmod` 가 바뀐 문서만 다시 받습니다. 내용이 같으면 저장하지 않습니다.
- **정기 실행:** `scripts/systemd/brcm-docs.{service,timer}` (매주 토요일 01시, 최대 20시간)

## 내보내기

```bash
./brcm-docs export                       # dist/brcm-docs-sqlite-YYYYMMDD.tar.gz  (corpus.sqlite + manifest.json)
./brcm-docs export -f md                 # 문서 1건 = .md 1개 (PAIS 데이터 소스 업로드용)
./brcm-docs export -f jsonl              # 섹션 1개 = 1줄 (임베딩 파이프라인용)
./brcm-docs export -f md -s kb --product "vSphere|ESXi|vSAN"   # 출처·제품 제한
```

## 데이터 구조 (`data/corpus.sqlite`)

| 테이블 | 내용 |
|---|---|
| `doc` | 문서 (id: `kb:312060`, `td:<경로>`, `gh:vmware/<repo>:<path>`, `url:<URL>`, `local:<sha>`) — 제목·URL·제품·수정일·상태(ok/skipped/gone) |
| `section` | 문서의 섹션 (제목 경로 + 본문). KB 는 Issue / Environment / Cause / Resolution 단위 |
| `fts` | 섹션 전문검색 (FTS5, porter) |
| `queue` | 수집 대기열 (URL·lastmod·상태·오류) — 내보내기에는 포함되지 않음 |

## 설정

`config/sources.yaml` 이 기본값입니다. 바꿀 값만 `config/local.yaml` 에 같은 구조로 적습니다 (Git 제외).

```yaml
# config/local.yaml 예
delay: 3
kb:
  scope: all
techdocs:
  prefixes:
    - https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/
    - https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/8-0/
```

## 시험

```bash
python3 tests/test_e2e.py     # 로컬 가짜 사이트·git 저장소로 전체 흐름 확인 (실제 사이트 접속 없음)
```

작업 기록: [docs/WORKLOG.md](docs/WORKLOG.md)
