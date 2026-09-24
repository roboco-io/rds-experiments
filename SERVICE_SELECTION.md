# DSQL 비교를 위한 RDS·Aurora 실험 대상 선정

> 조사일: 2026-09-24 · 검색: Exa `web_search_exa` · 주요 원문 확인: Exa `web_fetch_exa`
> 개정: 2026-09-24 · 사용자가 명확히 한 DSQL 중심 목적 반영 · DSQL 공식 문서 웹 재확인
> 핵심 목적: DSQL의 성능·개발/운영 편의성을 기존 인스턴스 기반·서버리스 서비스와 비교
> 업무 우선순위: ① 일반 웹서비스·SaaS OLTP → ② 트래픽 급증·유휴 → ③ 대용량 조회·집계

## 선정 결론

**주 대상은 DSQL(D1), 필수 대조군은 RDS PostgreSQL Multi-AZ DB 인스턴스(R1), Aurora PostgreSQL Provisioned(A1), Aurora PostgreSQL Serverless v2(A2)다.** DSQL을 최초 호환성 검사부터 성능·급증/유휴·편의성·비용 비교에 포함한다. RDS Multi-AZ DB 클러스터(R2), NVMe 기반 Optimized Reads와 스토리지 변형은 DSQL 채택 판단에 필요한 경우에만 추가한다.

PostgreSQL 계열을 선택한 이유는 DSQL과 가능한 한 같은 업무·데이터·클라이언트를 유지하면서 비교하고, 불가피한 SQL·인증·재시도 변경량을 직접 측정하기 위해서다. 서비스별 구조와 제약을 숨기지 않으며 PostgreSQL·MySQL 간 우열은 판단하지 않는다.

## 공식 문서에서 확인한 차이와 선정 판단

| 후보 | 확인한 서비스 특성 | 이 저장소의 선정 판단 |
| --- | --- | --- |
| Aurora DSQL | PostgreSQL 16 호환 분산 DB, OCC 기반 충돌 처리, 서울 단일 리전 지원 | **D1, 주 대상·필수.** 성능뿐 아니라 최초 연결·이관·확장·진단·복구·삭제 작업을 평가 |
| RDS PostgreSQL Multi-AZ DB 인스턴스 | primary와 장애 대응 standby; standby는 읽기 트래픽을 처리하지 않음 | **R1, 필수.** 일반적인 HA OLTP의 기준선. 읽기 복제본 비용을 기본 구성에 숨기지 않음 |
| RDS PostgreSQL Multi-AZ DB 클러스터 | writer 1개와 reader 2개, 3개 AZ; 반동기 복제이며 reader의 변경 적용 지연과 failover가 연결됨 | **R2, 선택 확장.** 읽기 확장·복구 방식이 DSQL 채택 결론에 영향을 줄 때 추가 |
| Aurora PostgreSQL Provisioned | DB 인스턴스와 분리된 공유 클러스터 스토리지, writer/reader 구성 | **A1, 필수.** 일정한 OLTP 부하에서 고정 용량의 성능·비용 기준선 |
| Aurora PostgreSQL Serverless v2 | 인스턴스별 ACU 증감, 지원 버전에서 0 ACU pause/resume; 서울 지원 | **A2, 필수.** 첫 OLTP부터 참여하는 기존 서버리스 대조군. DSQL과 급증·유휴·설정 부담 비교 |
| Aurora Standard / I/O-Optimized | 엔진이 아니라 스토리지·과금 구성. Standard는 I/O 요청 과금, I/O-Optimized는 읽기/쓰기 I/O 별도 과금 없음 | **선택적 비용 변수.** Standard 기준선 후 DSQL과의 비용 판단에 필요할 때 확대 |
| RDS PostgreSQL Optimized Reads | NVMe 기반 클래스에서 임시 작업을 로컬 스토리지로 처리; DB 인스턴스와 Multi-AZ DB 클러스터에 적용 | **3순위의 선택 확장.** 정렬·집계 튜닝이 DSQL 대비 판단을 바꾸는지 검사 |
| Aurora PostgreSQL Optimized Reads | NVMe의 임시 작업 공간; I/O-Optimized에서는 tiered cache도 사용 | **3순위의 선택 확장.** 큰 조회·집계와 OLTP 간섭의 원인 분리 |

근거: [RDS Multi-AZ 구분](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html), [RDS 클러스터 구조](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/multi-az-db-clusters-concepts.html), [Aurora 스토리지](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Overview.StorageReliability.html), [Serverless 지원 리전](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.Aurora_Fea_Regions_DB-eng.Feature.ServerlessV2.html), [자동 pause](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html), [RDS Optimized Reads](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PostgreSQL.optimizedreads.html), [Aurora Optimized Reads](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.optimized.reads.html), [DSQL 호환성](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with.html), [DSQL 리전](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/what-is-aurora-dsql.html).

표의 선정 판단은 사용자 우선순위에 따른 실험 설계다. AWS가 게시한 성능 개선 배율을 이 저장소의 예상 결과나 합격 기준으로 사용하지 않는다.

## DSQL 중심 비교에서 확인할 편의성

DSQL의 인프라 관리 자동화가 실제 개발·운영 작업 감소로 이어지는지는 실험으로 확인한다. IAM 기반 연결, SQL 이관, 충돌 재시도, 지원되는 복구 절차 때문에 추가되는 작업도 같은 기준으로 기록한다. 공식 기능 설명을 측정된 작업 시간이나 성능 결과로 대체하지 않는다.

- 최초 생성·인증·스키마 배포부터 첫 정상 거래까지 경과 시간과 실제 작업 시간을 분리한다. [DSQL 인증](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/authentication-authorization.html)
- 공통 업무의 SQL·앱 변경, 충돌 처리와 풀 설정의 수정량·검증 시간을 비교한다. [이관 안내](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-postgresql-compatibility-migration-guide.html), [동시성 제어](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-concurrency-control.html)
- 확장·문제 진단·복구·삭제를 같은 완료 조건으로 평가하고, 직접 수행할 수 없는 작업과 서비스가 대신 관리하는 작업을 구분한다. [DSQL 구조](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/what-is-aurora-dsql.html), [DSQL 복원](https://docs.aws.amazon.com/aws-backup/latest/devguide/restore-auroradsql.html)
- 같은 부하의 DSQL DPU·저장 비용과 대조군 전체 구성 비용을 비교하고 작업 시간은 별도 표시한다. [DSQL 과금](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/billing-metering.html)

측정 절차와 판정 기준은 [E011](EXPERIMENT_PLAN.md#e011), 성능·비용 비율은 [실험 계획](EXPERIMENT_PLAN.md)에 정의한다.

## 대용량 조회에서 분리할 변수

기본 D1/R1/A1/A2 조회 비교 후 튜닝 원인을 분리할 필요가 있으면 Aurora에 다음 2×2 비교를 사용한다. 이 변형 안에서는 노드 수, PostgreSQL 버전, vCPU·메모리, 데이터·SQL·인덱스를 같게 유지한다. 이를 DSQL과 동일 하드웨어 조건이라는 뜻으로 해석하지 않는다. 각 클래스의 서울 리전 주문 가능 여부를 실행 전에 확인한다.

| 구성 ID | 클래스 후보 | 스토리지 구성 | 목적 |
| --- | --- | --- | --- |
| A1 | `db.r6g.xlarge` | Standard | 기본 대조군 |
| A1-IO | `db.r6g.xlarge` | I/O-Optimized | 스토리지·과금 구성 차이 |
| A3 | `db.r6gd.xlarge` | Standard | NVMe 임시 작업의 효과 |
| A3-IO | `db.r6gd.xlarge` | I/O-Optimized | NVMe 임시 작업과 tiered cache를 포함한 구성 |

RDS에는 R1의 NVMe 변형 `R1-NVMe`와 이미 NVMe를 사용하는 R2를 포함한다. 지원되는 세션에서는 `temp_tablespaces`를 로컬 임시 공간/기본 공간으로 바꾸어 임시 I/O 효과를 분리한다. Aurora의 tiered cache와 RDS의 임시 작업 최적화를 같은 기능으로 취급하지 않는다. [RDS 임시 공간 제어](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PostgreSQL.optimizedreads.html), [Aurora 기능 조합](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.optimized.reads.html)

이 구성들은 전 실험에 곱하지 않는다. [E012](EXPERIMENT_PLAN.md#e012)의 조회·집계와 [E010](EXPERIMENT_PLAN.md#e010)의 비용 분석에만 우선 사용한다.

## 이번 단계에서 보류하는 후보

| 후보 | 보류 이유와 재검토 조건 |
| --- | --- |
| RDS PostgreSQL Single-AZ | 운영 HA 비교군과 요구사항이 다름. HA의 추가 비용·지연이 필요할 때 R0 대조군으로만 사용 |
| RDS MySQL / Aurora MySQL | SQL·옵티마이저 차이가 추가됨. PostgreSQL 실험 뒤 같은 업무 요구를 엔진별로 구현하는 독립 트랙으로 확장 |
| Aurora PostgreSQL Limitless Database | 전용 `16.X-limitless` 엔진, DB shard group, I/O-Optimized 구성 필요. 단일 writer의 쓰기·저장 한계를 넘는 요구가 확인될 때 shard key·분산 JOIN·트랜잭션 실험을 별도로 설계 |
| Aurora Global Database / DSQL 다중 리전 | 이번 우선순위에는 리전 간 active-active 또는 리전 전체 DR 요구가 없음. 해당 요구가 생기면 지리적 지연·복구·추가 비용을 별도 비교 |
| RDS MariaDB / Oracle / SQL Server / Db2 | 기존 엔진 의존성·전용 기능·라이선스 조건이 주어지지 않음. 이관할 실제 애플리케이션이 정해지면 포함 여부 재검토 |
| Redshift 등 분석 전용 서비스 | 현재는 OLTP가 우선이며 조회·집계가 OLTP에 미치는 영향을 조사. 분석 분리 자체가 의사결정이 되면 별도 비교 |

기능·제품 범위 근거: [AWS 데이터베이스 선택 안내](https://docs.aws.amazon.com/databases-on-aws-how-to-choose/), [Limitless 요구사항](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/limitless-reqs-limits.html).

## 지역·버전과 조사 범위

- 기본 리전은 서울이다. RDS Multi-AZ DB 클러스터와 Aurora Serverless의 PostgreSQL 16 계열 지원을 문서에서 확인했다. 공통 major 16으로 실험하고 실제 제공되는 minor·Aurora 패치·Serverless 플랫폼 버전을 실행 명세에 고정한다. [RDS 지원표](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.RDS_Fea_Regions_DB-eng.Feature.MultiAZDBClusters.html), [Serverless 지원표](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.Aurora_Fea_Regions_DB-eng.Feature.ServerlessV2.html)
- Optimized Reads의 사용 가능 여부는 지원 엔진과 NVMe 클래스의 리전 제공 여부에 달려 있다. 공식 기능 지원과 해당 계정에서 즉시 주문 가능한 구성은 구분한다.
- Exa에서 배포 방식, Serverless·스토리지, Optimized Reads, DSQL·Limitless의 공식 문서를 검색하고 주요 원문을 확인했다. 서비스 홍보의 최고 성능 수치는 선정 근거에서 제외했다.
- 실계정 조회·생성·벤치마크는 수행하지 않았다. 가격은 실제 선택한 버전·구성·실행 시점의 서울 단가로 E010에서 산출한다.

실행 절차와 지표는 [실험 계획](EXPERIMENT_PLAN.md), 상태는 [실험 목록](experiments/README.md)에서 관리한다.
