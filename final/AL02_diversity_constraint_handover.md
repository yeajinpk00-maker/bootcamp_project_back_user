# AL-02 추천 다양성 제약 도입 핸드오버

> **대상**: Claude Code / 백엔드·알고리즘 구현 담당자  
> **목적**: 동일 체인 및 동일 카테고리 후보가 일정에 반복적으로 배정되는 문제를 해결한다. 기존 AHP 기반 relevance는 유지하고, S3(날짜별 장소 배정)와 alternatives API에 일관된 **다양성 제약·재순위화 정책**을 도입한다.  
> **기준 문서**: AL-02 기능명세서 v2.9, ERD v15  
> **작성일**: 2026-09-11

---

## 1. 작업 요약

### 해결하려는 문제

프론트 실데이터에서 사용자가 `쇼핑`을 선호 카테고리로 선택했을 때, 1일차가 다음처럼 동일 체인(올리브영) 매장으로 채워지는 현상이 확인되었다.

```text
올리브영 개봉역북부점
올리브영 고척아이파크몰점
올리브영 구일역점
올리브영 가산점
올리브영 가산디지털단지역점
올리브영 남구로점
올리브영 구로구청점
```

원인은 기존 추천이 후보별 `relevance`를 중심으로 동작하고, **이미 선택된 장소와의 카테고리·체인 중복을 판단하지 않기 때문**이다. 쇼핑 후보의 약 89%(1,072/1,201)가 올리브영이라는 데이터 편향도 확인되었다.

### 최종 목표

1. 기존 AHP relevance를 유지한다.
2. 동일 장소는 전체 여행에서 중복 추천하지 않는다.
3. 카테고리별 쏠림을 막아, 하루와 전체 여행에 다양한 경험이 분배되도록 한다.
4. 쇼핑은 선호 순위가 높더라도 전체 여행에서 기본적으로 1곳만 추천한다.
5. 브랜드 DB 모델이 없는 현 상태에서도 명확한 체인(예: 올리브영)은 보조적으로 중복 차단한다.
6. `/recommend`와 `/alternatives`가 같은 정책·공용 함수를 사용하게 한다.
7. 공연일의 시간 예산·공연 마지막 고정·공연 전 150분 버퍼는 기존과 동일하게 하드 제약으로 유지한다.

---

## 2. ERD 및 제약

### 2.1 ERD v15 확인 결과

현재 DB에는 브랜드를 식별하는 정식 모델이 없다.

| 엔터티/컬럼 | 존재 | 용도 |
|---|---:|---|
| `event.eventno` | O | 장소/이벤트 PK |
| `event.eventnm` | O | 장소명 문자열 |
| `event.ctgno` | O | 세부 카테고리 FK |
| `ctg.ctgtypeno` | O | 상위 카테고리 유형 FK |
| `tripinterest.ctgno`, `tripinterest.rank` | O | 여행별 선호 카테고리와 순위 |
| `triprouteevent.eventno` | O | 일정에 저장된 장소 |
| `brand`, `brand_no`, `brand_key` | **X** | 정식 브랜드 식별 구조 없음 |

**중요**: `eventnm`은 PK가 아니므로, 이벤트 조인·저장·동일 장소 판정에는 사용하지 않는다. 동일 장소 여부는 반드시 `eventno`로 판정한다. `eventnm`은 당분간 명확한 체인 중복을 줄이기 위한 **런타임 보조 키**로만 사용할 수 있다.

### 2.2 현 단계 판단

브랜드 테이블을 즉시 추가하지 않아도, 아래 데이터만으로 카테고리 다양성의 핵심 정책은 구현 가능하다.

- 동일 장소: `eventno`
- 세부 카테고리 중복: `ctgno`
- 상위 유형 분포 확인: `ctgtypeno`
- 사용자 선호 우선순위: `tripinterest.rank`
- 여행 전체·날짜별 선택 이력: `triprouteevent` + `triproute`

브랜드 반복 방지는 다음의 2단계로 운영한다.

- **단기**: 코드의 allow-list 기반 `derive_brand_key(eventnm)`으로 올리브영 등 명확한 체인만 판별
- **중기 이후**: 필요성이 확인되면 `event.brand_key NULL` 또는 `brand`/`event.brand_no` 구조를 별도 데이터 모델 작업으로 도입

이번 작업은 DB 마이그레이션을 필수로 하지 않는다.

---

## 3. 학술·방법론 근거

### 3.1 문제 정의

이 문제는 후보별 적합도만 최적화하는 **Top-N 추천**의 중복성 문제다. 같은 카테고리와 체인이 많이 포함된 후보군에서는 relevance 내림차순만으로는 추천 목록이 단조로워질 수 있다.

따라서 AL-02는 다음 목표를 동시에 다룬다.

\[
\text{여행 품질} = \text{사용자 취향 적합도} + \text{선호 커버리지} + \text{목록 다양성} - \text{이동 비용}
\]

이는 관광 경로 최적화(TTDP)에서 POI 효용, 이동·시간 제약, 카테고리 제약을 함께 고려하는 확장과 정합적이다.

### 3.2 MMR: 관련성-중복성 균형

Carbonell & Goldstein (1998)의 **MMR(Maximal Marginal Relevance)** 은 후보의 관련성을 유지하면서, 이미 선택된 항목과 유사한 후보의 우선순위를 낮춰 중복을 줄이는 재순위화 방법이다.

\[
\operatorname{MMR}(p,S)
=
\lambda \cdot \operatorname{relevance}(p)
-
(1-\lambda) \cdot \max_{s \in S}\operatorname{similarity}(p,s)
\]

- `p`: 새로 선택할 후보
- `S`: 이미 여행 일정에 선택된 장소 집합
- `relevance(p)`: 기존 AL-02 AHP 점수
- `similarity(p,s)`: 카테고리·체인 등 중복 정도
- `λ`: relevance와 다양성의 균형 계수

AL-02에서는 MMR을 완전한 단독 목적함수로 대체하지 않고, **기존 relevance + 카테고리 커버리지 보너스 − 중복 페널티**의 S3 재순위화 방식으로 응용한다.

### 3.3 xQuAD: 미충족 선호 카테고리 커버리지

Santos, Macdonald & Ounis (2010)의 **xQuAD(Explicit Query Aspect Diversification)** 는 사용자의 여러 잠재 관심사(aspect) 중 아직 추천 목록에서 충족되지 않은 관심사를 가진 후보에 가치를 부여한다.

AL-02에서 aspect는 `tripinterest`에 저장된 1·2·3순위 선호 카테고리로 해석한다.

예시:

```text
사용자 선호: 1순위 쇼핑, 2순위 생일카페, 3순위 유적지
이미 선택: 쇼핑 1곳
아직 미선택: 생일카페 0곳, 유적지 0곳
```

이 경우 쇼핑 후보를 반복 선택하기보다, 아직 포함되지 않은 생일카페·유적지 후보에 보너스를 부여해 선호의 **coverage**를 높인다.

### 3.4 TTDP: 카테고리 상·하한은 정식 제약으로 모델링 가능

관광 일정 추천은 Tourist Trip Design Problem(TTDP)의 한 유형이다. TTDP 문헌은 POI별 카테고리를 두고, 시간 예산·방문 시간창·필수 방문지와 함께 카테고리별 방문 횟수의 상한/하한을 제약으로 모델링할 수 있음을 제시한다.

따라서 다음은 임의의 예외처리가 아니라, TTDP 제약 기반의 서비스 정책이다.

\[
\sum_{p \in S} \mathbb{1}[ctg(p)=c] \leq U_c
\]

- `c`: 카테고리
- `U_c`: 해당 카테고리의 방문 상한

### 3.5 이 문서에서의 구분

| 구분 | 근거 | FAN:GO 적용 |
|---|---|---|
| relevance와 중복성의 동시 고려 | MMR | 중복 후보 감점/재순위화 |
| 여러 선호의 균형 있는 충족 | xQuAD | 미방문 선호 카테고리 보너스 |
| 카테고리 상한·시간 제약 | TTDP | 일자별·여행 전체 상한 및 시간 검증 |
| 구체 수치(비율·보너스 등) | 서비스 정책값 | 로그·실측을 통해 후속 보정 |

**주의**: 논문은 ‘관련성·커버리지·다양성·제약을 함께 고려하는 방법론’을 뒷받침한다. `35%`, `0.15`, `1곳` 같은 정확한 숫자를 논문이 보편값으로 제공하는 것은 아니다. 아래 숫자는 FAN:GO의 초기 정책값이며 실험·로그로 재보정한다.

---

## 4. 확정 정책안

### 4.1 우선순위

제약 충돌 시 우선순위는 다음과 같다.

1. 데이터 유효성 및 기존 하드 제약: 운영 상태, `eventno` 유효성, 영업시간, 일일 시간 예산, 공연 마지막 고정, 공연 전 150분 버퍼
2. 중복 금지: 동일 `eventno`, 동일 체인(확실히 판별 가능한 경우), 쇼핑 전체 상한
3. 카테고리 다양성: 일자별 상한, 전체 일정 상한
4. 사용자 취향 relevance 및 카테고리 커버리지
5. 이동시간 최소화/동선 적합도
6. 목표 방문 수 충족

즉, 목표 방문 수를 채우기 위해 동일 체인·쇼핑을 반복하거나 공연 버퍼를 침해해서는 안 된다.

### 4.2 하드 제약

```python
MAX_SAME_EVENT_PER_TRIP = 1
MAX_SAME_BRAND_PER_TRIP = 1      # brand_key를 신뢰성 있게 얻은 경우만 적용
MAX_SHOPPING_PER_TRIP = 1
MAX_SAME_CATEGORY_PER_DAY = 1
```

| ID | 제약 | 기준 | 기본 정책 | 비고 |
|---|---|---|---:|---|
| D01 | 동일 장소 중복 | `eventno` | 전체 여행 최대 1회 | 필수 |
| D02 | 동일 브랜드 중복 | 런타임 `brand_key` | 전체 여행 최대 1회 | 판별 가능한 체인에만 적용 |
| D03 | 쇼핑 반복 | 쇼핑 `ctgno` 집합 | 전체 여행 최대 1곳 | 브랜드가 달라도 적용 |
| D04 | 동일 세부 카테고리 반복 | `ctgno` | 하루 최대 1곳 | 기본 정책 |
| D05 | 공연 제약 | 공연 event | 해당 일자 마지막 | 기존 유지 |
| D06 | 공연 안전 버퍼 | 시간 | 공연 시작 150분 전 도착 | 기존 유지 |
| D07 | 일일 시간 예산 | 시간 | 09:00~21:00 | 기존 유지 |

### 4.3 전체 일정 카테고리 상한: 비율 + 절대 상한

여행 길이·동선 스타일·공연 여부에 따라 최종 장소 수가 달라지므로, 전체 일정의 카테고리 상한은 고정값만 쓰지 않는다.

공연을 제외한 일반 POI의 **목표 방문 수** `N_non_concert_target`을 먼저 계산하고, 다음 식으로 상한을 만든다.

\[
U_c = \min\left(A_c,\; \max\left(1,\left\lceil r_c \cdot N_{\text{non-concert-target}}\right\rceil\right)\right)
\]

| 변수 | 의미 |
|---|---|
| `N_non_concert_target` | 공연을 제외한 여행 전체 목표 방문 수 |
| `r_c` | 카테고리별 최대 허용 비율 |
| `A_c` | 절대 상한 |
| `U_c` | 여행 전체에서 카테고리 `c`가 가질 수 있는 최대 개수 |

#### 초기 정책값

| 대상 | 비율 `r_c` | 절대 상한 `A_c` | 전체 여행 상한 |
|---|---:|---:|---|
| 1순위 선호 일반 카테고리 | 0.35 | 2 | 위 수식 적용 |
| 2순위 선호 일반 카테고리 | 0.30 | 2 | 위 수식 적용 |
| 3순위 선호 일반 카테고리 | 0.25 | 2 | 위 수식 적용 |
| 미선택 일반 카테고리 | 0.20 | 1 | 위 수식 적용 |
| 쇼핑 | 0.20 | **1** | 항상 최대 1곳 |

예를 들어 일반 POI 목표가 7곳이면, 1순위 일반 카테고리는 `ceil(7×0.35)=3`이나 절대 상한 2가 적용되어 최대 2곳이다. 쇼핑은 `ceil(7×0.20)=2`여도 절대 상한 1이 적용된다.

### 4.4 하루 상한 및 완화

기본은 하루 동일 `ctgno` 1곳이다. 하루 2곳을 일반 기본값으로 사용하지 않는다.

| 상황 | 기본 하루 상한 | 완화 |
|---|---:|---|
| 공연일 | 1 | 완화하지 않음 |
| 여유롭게 비공연일 | 1 | 완화하지 않음 |
| 기본 비공연일 | 1 | 후보 부족 시만 일반 카테고리 2곳 허용 가능 |
| 빽빽하게 비공연일 | 1 | 목표가 7곳이고, 다른 카테고리 유효 후보가 부족할 때만 일반 카테고리 2곳 허용 |
| 쇼핑 | 1 | 전체 여행 상한 1을 절대 완화하지 않음 |
| 동일 브랜드 | 1 | 전체 여행 상한 1을 절대 완화하지 않음 |

### 4.5 목표 방문 수 산정

카테고리 상한은 결과가 나온 뒤 계산하면 늦다. S3 후보 배정 전에 `target_visits_by_day`와 `N_non_concert_target`을 만들 것.

```python
DENSITY_TARGET_RANGE = {
    "relaxed":  {"normal": (3, 4), "concert": (2, 3)},
    "balanced": {"normal": (4, 5), "concert": (3, 4)},
    "dense":    {"normal": (5, 7), "concert": (3, 4)},
}
```

목표값은 다음을 고려해 선택한다.

- 동선 스타일 (`tripdensityno`)
- 비공연일/공연일 여부
- 기본 체류시간(STAY_MAP)
- 09:00~21:00 활동시간
- 공연일의 공연 기본 체류시간과 150분 버퍼
- 시작·종료 depot 및 실제/캐시 이동시간

S4에서 실제 이동시간으로 장소가 제거되어 실제 수가 줄었다면, S5에서 **실제 최종 방문 수**로 카테고리 분포를 다시 검증한다.

---

## 5. 구현 설계

### 5.1 공용 모듈화 원칙

`/recommend`와 `/alternatives`는 서로 다른 엔드포인트여도 아래 함수를 같은 공용 모듈에서 호출해야 한다. alternatives에서 `artist_group_map`을 비워 relevance가 상수화된 과거 결함처럼, 정책 로직이 두 경로에서 갈라지는 것을 막기 위함이다.

```python
build_trip_context(...)
calculate_relevance(...)
derive_brand_key(...)
make_diversity_caps(...)
can_insert_candidate(...)
calculate_selection_score(...)
validate_final_diversity(...)
```

권장 파일 구조(프로젝트 실제 구조에 맞춰 조정):

```text
al02_policy.py          # 정책 상수, 카테고리 분류, brand allow-list
al02_diversity.py       # diversity context, caps, 조건 검사, 점수
al02_pipeline.py        # S3/S4/S5에서 공용 함수 호출
al02_alternatives.py    # 공용 함수 호출; 교체 대상 제거 상태 적용
al02_selftest.py        # 회귀 테스트
```

### 5.2 런타임 브랜드 키

정식 DB 필드가 없으므로 초기에는 명확한 체인만 allow-list로 판별한다.

```python
KNOWN_CHAIN_PREFIXES = {
    "올리브영": "oliveyoung",
    "다이소": "daiso",
    "스타벅스": "starbucks",
}

def derive_brand_key(event_nm: str | None) -> str | None:
    if not event_nm:
        return None

    normalized = normalize_event_name(event_nm)

    for prefix, key in KNOWN_CHAIN_PREFIXES.items():
        if normalized.startswith(prefix):
            return key

    return None
```

#### 필수 조건

- `eventnm` 문자열로 DB 조인·저장·동일 장소 판정 금지
- `brand_key`가 `None`이면 브랜드 제약을 적용하지 않음
- 모든 장소명의 첫 토큰을 브랜드로 간주하지 않음
- allow-list에 없는 상호는 잘못 묶지 않는 편을 우선
- `brand_key`는 디버깅·다양성 제약용 파생 필드임을 명시

### 5.3 카테고리 분류

쇼핑 판단은 `eventnm`이 아니라 `ctgno`로 한다.

```python
SHOPPING_CTG_NOS = {...}  # ctg 테이블의 실제 쇼핑 ctgno로 확정할 것
```

구현 전 반드시 아래를 확인한다.

```sql
SELECT c.ctgno, c.ctgnm, c.ctgtypeno, ct.ctgtypenm
FROM ctg c
JOIN ctgtype ct ON ct.ctgtypeno = c.ctgtypeno
ORDER BY c.ctgtypeno, c.ctgno;
```

`SHOPPING_CTG_NOS`는 코드 하드코딩보다 `al02_policy.py`에 정책 상수로 두고, 주석에 기준 카테고리를 기록한다.

### 5.4 다양성 컨텍스트

```python
@dataclass
class DiversityState:
    selected_event_nos: set[int]
    trip_category_counts: Counter[int]
    trip_brand_counts: Counter[str]
    shopping_count: int

@dataclass
class DayDiversityState:
    category_counts: Counter[int]
    selected_event_nos: set[int]

@dataclass
class DiversityCaps:
    day_category_cap: int
    trip_category_caps: dict[int, int]
    max_shopping_per_trip: int = 1
    max_same_brand_per_trip: int = 1
```

### 5.5 삽입 가능 여부: 단일 공용 함수

S3의 일반 배정과 alternatives의 교체 가능 여부가 반드시 이 함수를 재사용하게 한다.

```python
def can_insert_candidate(
    candidate,
    day_state: DayDiversityState,
    trip_state: DiversityState,
    caps: DiversityCaps,
    *,
    is_concert_day: bool,
    allow_day_category_relaxation: bool,
) -> tuple[bool, str | None]:
    """후보가 다양성 제약을 통과하는지 판정한다.

    시간 예산·공연 버퍼 검사 자체는 기존 함수와 결합하거나 호출부에서 추가한다.
    반환 reason은 로그·QA·API 디버깅용이다.
    """

    if candidate.event_no in trip_state.selected_event_nos:
        return False, "duplicate_event"

    brand_key = derive_brand_key(candidate.event_nm)
    if brand_key and trip_state.trip_brand_counts[brand_key] >= caps.max_same_brand_per_trip:
        return False, "brand_trip_cap"

    if candidate.ctg_no in SHOPPING_CTG_NOS:
        if trip_state.shopping_count >= caps.max_shopping_per_trip:
            return False, "shopping_trip_cap"

    day_cap = 1
    if allow_day_category_relaxation and not is_concert_day:
        day_cap = caps.day_category_cap

    if day_state.category_counts[candidate.ctg_no] >= day_cap:
        return False, "category_day_cap"

    trip_cap = caps.trip_category_caps.get(candidate.ctg_no, 1)
    if trip_state.trip_category_counts[candidate.ctg_no] >= trip_cap:
        return False, "category_trip_cap"

    return True, None
```

**구현 주의**: `day_category_cap`의 기본값은 1이다. 2로 완화하는 경우는 빽빽한 비공연일의 2차 배정에서만 허용해야 한다.

### 5.6 S3: 2단계 배정

#### 1차 배정: 다양성 우선

- 모든 하드 제약과 기본 카테고리 상한 적용
- 선호 카테고리 중 아직 선택되지 않은 카테고리에 coverage bonus 부여
- 같은 카테고리·같은 체인 후보는 중복 페널티 또는 하드 차단
- 이 단계에서 목표 방문 수를 채우지 못해도, 쇼핑·동일 브랜드 상한은 풀지 않는다.

#### 2차 배정: 제한적 완화

다음 조건을 모두 만족할 때에만 일반 카테고리의 하루 상한을 2로 완화한다.

- 비공연일
- 동선 스타일이 `dense`
- 해당 날짜 목표 방문 수가 7
- 1차 배정에서 목표를 충족하지 못함
- 아직 다른 카테고리의 유효 후보가 충분하지 않음

쇼핑 전체 1곳, 동일 브랜드 전체 1곳, 동일 장소 전체 1곳은 2차에서도 절대 완화하지 않는다.

```python
schedule = run_assignment(allow_day_category_relaxation=False)

if (
    density == "dense"
    and not is_concert_day
    and day_target_visits >= 7
    and len(schedule) < day_target_visits
    and no_valid_new_category_candidate_remains(...)
):
    schedule = fill_with_limited_category_relaxation(schedule)
```

### 5.7 선택 점수: relevance 유지 + coverage + 중복 감점

기존 relevance 공식은 변경하지 않는다.

\[
relevance(p)=0.6483\cdot artist\_match(p)
+0.2297\cdot category\_fitness(p)
+0.1220\cdot place\_quality(p)
\]

S3 배정용 점수는 별도로 계산한다.

\[
selection\_score(p,S)=relevance(p)+coverage\_bonus(p,S)-redundancy\_penalty(p,S)+route\_fit(p)
\]

초기 구현값:

```python
COVERAGE_BONUS_BY_RANK = {
    1: 0.15,
    2: 0.10,
    3: 0.05,
}
MMR_REDUNDANCY_WEIGHT = 0.10
```

- `coverage_bonus`: 아직 여행 전체에 포함되지 않은 선호 카테고리라면 해당 순위의 보너스
- `redundancy_penalty`: 하드 제약으로 걸러지지 않은 후보에 대해서만 적용
- `route_fit`: 기존 S3/S4 이동시간 로직을 중복 구현하지 말고, 기존 삽입 증가 이동시간 평가 결과를 이용

예시 유사도:

```python
def similarity(candidate, selected) -> float:
    if candidate.event_no == selected.event_no:
        return 1.0

    c_brand = derive_brand_key(candidate.event_nm)
    s_brand = derive_brand_key(selected.event_nm)
    if c_brand and c_brand == s_brand:
        return 1.0

    if candidate.ctg_no in SHOPPING_CTG_NOS and selected.ctg_no in SHOPPING_CTG_NOS:
        return 0.8

    if candidate.ctg_no == selected.ctg_no:
        return 0.6

    if candidate.ctg_type_no == selected.ctg_type_no:
        return 0.3

    return 0.0
```

**주의**: 위 0.15/0.10/0.05, 0.10, 0.8/0.6/0.3은 논문의 보편 상수가 아니다. 방법론에서 출발한 FAN:GO 초기 정책값이므로, 상수명·문서·테스트를 통해 추적 가능하게 관리한다.

### 5.8 S5: 실제 결과 기반 최종 검증

S4의 실제 이동시간 최적화에서 장소가 제거될 수 있으므로, S5 저장 전에 최종 경로를 다시 검증한다.

검증 항목:

- 전체 여행에 동일 `eventno` 중복 없음
- 체인으로 판별되는 동일 `brand_key` 중복 없음
- 쇼핑은 전체 1곳 이하
- 하루 동일 `ctgno` 상한 충족
- 전체 카테고리 비율+절대 상한 충족
- 공연일 마지막 공연 고정 및 150분 버퍼 충족
- 09:00~21:00 시간 예산 충족

위반 시 다음 순서로 처리한다.

1. 위반 카테고리/체인에서 selection score 또는 relevance가 가장 낮은 장소 제거
2. 다른 카테고리의 유효 후보로 대체 시도
3. 대체 후보가 없으면 장소 수를 줄임
4. 응답 또는 로그에 `insufficient_diverse_candidates` 기록

반복 쇼핑·동일 체인 장소를 넣어 목표 개수를 억지로 채우지 않는다.

---

## 6. Alternatives API 적용

### 6.1 현재 알려진 결함

기존 alternatives relevance 상수화는 `calc_relevance()` 호출 시 `artist_group_map={}`가 전달되어, `artist_no=NULL`, `artist_group_no`만 있는 그룹 태그 이벤트의 0.7 매칭이 0으로 처리된 것이 원인이었다. `/recommend`에 존재하는 `build_artist_group_map()` 호출을 `/alternatives`에도 적용해 수정된 것으로 보고되었다.

### 6.2 이번 작업의 필수 원칙

alternatives는 일반 후보 조회가 아니라, **현재 장소 하나를 바꾼 뒤에도 전체 여행이 유효한지 판단하는 기능**이다.

1. 현재 교체 대상 `target_event_no`를 일정 상태에서 임시 제거한다.
2. 제거된 상태로 `build_trip_context`, relevance, diversity state, caps를 재생성한다.
3. 후보마다 일반 S3와 같은 `can_insert_candidate()`를 호출한다.
4. 시간 예산·공연 버퍼까지 재검증한다.
5. 유효 후보만 정렬하여 반환한다.

```python
state_without_target = build_diversity_state(
    trip_no=trip_no,
    exclude_event_no=target_event_no,
)

day_state_without_target = build_day_diversity_state(
    trip_no=trip_no,
    visit_day=visit_day,
    exclude_event_no=target_event_no,
)

for candidate in candidates:
    valid, reason = can_insert_candidate(
        candidate,
        day_state_without_target,
        state_without_target,
        caps,
        is_concert_day=is_concert_day,
        allow_day_category_relaxation=False,
    )
    if not valid:
        continue

    if not satisfies_time_and_concert_constraints_after_replacement(...):
        continue
```

### 6.3 alternatives 점수 분리

기존 `relevance`는 취향 적합도이고 거리 그 자체를 포함하지 않는다. 교체 UI에서는 거리·실제 동선 영향을 반영한 별도 점수를 반환한다.

\[
replacement\_score(a)=0.60\cdot base\_relevance(a)
+0.25\cdot travel\_fit(a)
+0.15\cdot diversity\_fit(a)
\]

여기서 `travel_fit`은 단순 `distance_km`보다 교체 전후의 **실제 추가 이동시간**을 사용한다.

\[
added\_travel\_min = T(prev,a)+T(a,next)-T(prev,target)-T(target,next)
\]

- `prev`: 교체 대상 직전 장소/depot
- `target`: 기존 장소
- `a`: 대체 후보
- `next`: 교체 대상 다음 장소/공연/depot
- `T`: 카카오모빌리티 API 캐시 또는 기존 이동시간 함수

권장 초기 `travel_fit`:

| 추가 이동시간 | `travel_fit` |
|---:|---:|
| 0분 이하 | 1.00 |
| 1~10분 | 0.85 |
| 11~20분 | 0.65 |
| 21~30분 | 0.40 |
| 31분 초과 | 0.00 또는 후보 제외 |

`replacement_score`의 0.60/0.25/0.15도 초기 서비스 정책값이다. 먼저 구현·로그 수집 후 가중치를 조정한다.

### 6.4 API 응답 권장 확장

기존 응답 호환성을 깨지 않도록 기존 `relevance`는 유지하고, 디버깅·프론트 활용을 위해 선택 필드를 추가한다.

```json
{
  "event_no": 1234,
  "event_nm": "다이소 마리오아울렛점",
  "ctg_no": 8,
  "ctg_nm": "쇼핑",
  "distance_km": 2.1,

  "relevance": 0.412,
  "base_relevance": 0.412,
  "replacement_score": 0.684,
  "display_score": 68,
  "added_travel_min": 7,

  "score_breakdown": {
    "artist_match": 0.0,
    "category_fitness": 1.0,
    "place_quality": 0.72,
    "travel_fit": 0.85,
    "diversity_fit": 1.0
  },

  "constraint_check": {
    "duplicate_event_ok": true,
    "brand_trip_cap_ok": true,
    "shopping_trip_cap_ok": true,
    "category_day_cap_ok": true,
    "category_trip_cap_ok": true,
    "time_budget_ok": true,
    "concert_buffer_ok": true
  }
}
```

프론트 사용자 화면은 필요에 따라 `display_score`, `added_travel_min`, `distance_km`만 사용해도 된다. `score_breakdown`, `constraint_check`는 QA/개발 모드 또는 선택 반환 필드로 운용 가능하다.

---

## 7. 후보 부족 및 정책 완화

다양성 제약 적용 후에는 일부 지역·일정에서 목표 방문 수를 채우지 못할 수 있다. 이는 반복 장소를 넣어 해결하지 않는다.

### 완화 순서

1. 아직 사용하지 않은 카테고리의 유효 후보 탐색
2. 일반 카테고리만 전체 상한 범위 내에서 추가 탐색
3. `dense` + 비공연일 + 목표 7곳 + 다른 카테고리 후보 부족일 때만 해당 날짜의 일반 카테고리 상한을 2로 완화
4. 쇼핑 전체 1회, 동일 브랜드 전체 1회, 동일 장소 전체 1회 제약은 유지
5. 여전히 부족하면 추천 장소 수를 줄이고 warning을 반환

```json
{
  "warning": {
    "code": "insufficient_diverse_candidates",
    "message": "이동시간과 장소 다양성을 우선해, 목표보다 적은 장소로 일정이 구성되었습니다."
  },
  "diversity_relaxed": false
}
```

하루 카테고리 완화가 실제로 발생했다면:

```json
{
  "diversity_relaxed": true,
  "relaxed_rule": "category_day_cap",
  "relaxed_from": 1,
  "relaxed_to": 2
}
```

---

## 8. 구현·테스트 요구사항

### 8.1 필수 회귀 테스트

| ID | 테스트 입력/상황 | 기대 결과 |
|---|---|---|
| T01 | 같은 `eventno`가 여러 경로/일자에서 후보로 등장 | 전체 여행 최종 선택은 1회 |
| T02 | 올리브영 지점 다수 + 다른 카테고리 후보 존재 | 전체 여행 올리브영 최대 1곳 |
| T03 | 올리브영·다이소·무신사 등 쇼핑 후보 다수 + 쇼핑 1순위 | 쇼핑 카테고리 전체 최대 1곳 |
| T04 | 특정 일반 카테고리 후보 다수, 비공연일 | 하루 동일 `ctgno` 최대 1곳 |
| T05 | dense 비공연일, 목표 7곳, 다른 카테고리 후보 부족 | 조건 충족 시에만 일반 `ctgno` 최대 2곳, 완화 로그 기록 |
| T06 | 공연일 | 동일 `ctgno` 하루 최대 1곳, 공연 마지막, 150분 버퍼 유지 |
| T07 | 1·2·3순위 선호 카테고리 각각 후보 존재 | 1차 배정에서 각 미충족 선호 카테고리의 첫 진입 우대 |
| T08 | 최종 S4에서 이동시간 때문에 후보가 제거됨 | S5가 실제 최종 수 기준으로 다양성 재검증 |
| T09 | 교체 대상이 유일한 쇼핑 장소 | alternatives에 다른 쇼핑 후보 1개는 허용 가능 |
| T10 | 다른 날에 쇼핑 장소가 이미 존재 | alternatives에서 쇼핑 후보 제외 |
| T11 | alternatives 그룹 태그 이벤트 (`artist_no=NULL`) | `/recommend`와 동일한 artist_group 매핑·relevance |
| T12 | alternatives 후보 간 거리·리뷰·카테고리가 다름 | `replacement_score`가 모두 같은 값으로 고정되지 않음 |
| T13 | 교체 후 공연 버퍼 또는 일일 시간 초과 | alternatives 후보에서 제외 |
| T14 | 체인 allow-list 미등록 장소 | `brand_key=None`, 브랜드 제약 미적용·오분류 없음 |

### 8.2 실데이터 검증

최소 아래 시나리오를 실제 DB/API로 검증한다.

1. 프론트가 보고한 `생일카페·유적지·쇼핑` 선호 조합과 동일한 trip 재현
2. 쇼핑 후보가 많은 지역에서 `/recommend` 실행
3. 1~2일차 전체에 쇼핑이 1개 이하인지 확인
4. 동일 체인(올리브영)이 1개 이하인지 확인
5. 하루별 고유 `ctgno` 개수 확인
6. `/alternatives?visit_day=`에서 교체 후보가 동일한 제약을 지키는지 확인
7. 대체 후 실제 이동시간·시간 예산·공연 버퍼가 유지되는지 확인
8. 기존 `al02_selftest.py` 전체 PASS 확인

### 8.3 관찰 로그

다음 로그/응답 메타데이터를 남긴다.

```python
excluded_reason_counts = Counter()
# duplicate_event
# brand_trip_cap
# shopping_trip_cap
# category_day_cap
# category_trip_cap
# time_budget_or_concert_buffer

metrics = {
    "target_visits": ...,
    "actual_visits": ...,
    "unique_categories": ...,
    "shopping_count": ...,
    "brand_counts": ...,
    "diversity_relaxed": ...,
    "total_travel_minutes": ...,
}
```

운영 이후 확인할 핵심 지표:

| 지표 | 목적 |
|---|---|
| 동일 체인 2회 이상 여행 비율 | 브랜드 제한이 실제 동작하는지 확인 |
| 쇼핑 2회 이상 여행 비율 | 쇼핑 상한의 효과 확인 |
| 하루 고유 카테고리 수 | 일자별 다양성 확인 |
| 목표 방문 수 대비 실제 방문 수 | 제약이 과도한지 판단 |
| `insufficient_diverse_candidates` 발생률 | 후보 데이터 부족 또는 정책 과도 여부 확인 |
| 총 이동시간/장소당 이동시간 | 다양화로 동선 품질이 악화되는지 확인 |
| alternatives 교체율·되돌림률 | 교체 후보 품질 평가 |
| 공연 150분 버퍼 위반 | 반드시 0건 |

---

## 9. 예상 부작용 및 대응

| 위험 | 원인 | 대응 |
|---|---|---|
| 평균 relevance 하락 | 최고 점수 후보 일부를 다양성 때문에 제외 | `base_relevance`와 `selection_score` 분리 기록, 사용자 반응으로 튜닝 |
| 목표 방문 수 미달 | 특정 지역의 후보가 특정 카테고리에 편중 | 반복 쇼핑으로 채우지 말고 제한적 완화 후 warning 반환 |
| 사용자가 쇼핑 중심 여행을 원함 | 기본 다양성 정책과 강한 특정 취향 충돌 | 현 버전 기본 다양성 우선; 향후 별도 ‘쇼핑 중심’ 옵션을 별도 정책으로 추가 검토 |
| 체인 오분류 | 이름 기반 브랜드 추정의 한계 | 명확한 allow-list만 적용, 미확실한 상호는 `None` 처리 |
| 동선 비효율 | 다른 카테고리를 고르느라 멀어짐 | S3 후보 삽입 가능성·추가 이동시간 평가 후 선택, S4 실도로 최적화 유지 |
| `/recommend`/`alternatives` 정책 불일치 | 엔드포인트별 별도 구현 | 공용 함수화 + 교차 회귀 테스트 |
| 수치 과적합 | 초기 가중치·비율이 실사용 데이터와 불일치 | 상수 중앙화, 추천/교체 로그 기반 재보정 |

---

## 10. 완료 기준

다음 조건을 모두 만족하면 완료로 판단한다.

- [ ] 기존 AHP relevance 계산식과 가중치 `0.6483 / 0.2297 / 0.1220`이 변경되지 않았다.
- [ ] 동일 `eventno`가 하나의 여행 결과에 2회 이상 저장되지 않는다.
- [ ] 쇼핑 카테고리는 하나의 여행 결과에 최대 1곳이다.
- [ ] allow-list에 포함된 동일 체인(예: 올리브영)은 하나의 여행 결과에 최대 1곳이다.
- [ ] 동일 `ctgno`는 기본적으로 하루 최대 1곳이다.
- [ ] 동일 카테고리 하루 2곳은 명시된 dense 비공연일의 후보 부족 상황에서만 허용된다.
- [ ] `/recommend`와 `/alternatives`가 공용 relevance·다양성 제약 함수를 호출한다.
- [ ] alternatives는 교체 대상을 제거한 일정 상태를 기준으로 전체 여행·당일 제약을 검사한다.
- [ ] alternatives가 `base_relevance`, `replacement_score`, `added_travel_min` 또는 동등한 디버깅 정보를 제공한다.
- [ ] 공연 마지막 고정, 150분 버퍼, 09:00~21:00 시간 예산이 회귀하지 않는다.
- [ ] 필수 테스트(T01~T14)와 기존 self-test가 PASS한다.
- [ ] 실재현 trip에서 올리브영/쇼핑 다중 추천이 재발하지 않는다.

---

## 11. 참고 문헌

1. Carbonell, J. G., & Goldstein, J. (1998). *The Use of MMR, Diversity-Based Reranking for Reordering Documents and Producing Summaries*. SIGIR 1998.
   - 관련성을 유지하면서 이미 선택된 항목과의 유사성을 낮추는 MMR 재순위화 원리.
   - https://www.cs.cmu.edu/~jgc/publication/The_Use_MMR_Diversity_Based_LTMIR_1998.pdf

2. Santos, R. L. T., Macdonald, C., & Ounis, I. (2010). *Exploiting Query Reformulations for Web Search Result Diversification*. WWW 2010.
   - xQuAD 계열의 aspect coverage, novelty, relevance 기반 다양화.
   - DOI: 10.1145/1772690.1772780

3. Santos, R. L. T., Peng, J., Macdonald, C., & Ounis, I. (2010). *Explicit Search Result Diversification through Sub-Queries*. ECIR 2010.
   - 사용자의 여러 관심사(aspect)를 명시적으로 커버하는 xQuAD 프레임워크.

4. Zhang, M., & Hurley, N. (2008). *Avoiding Monotony: Improving the Diversity of Recommendation Lists*. RecSys 2008.
   - 추천 목록 내부 다양성(Intra-List Diversity)과 관련성의 균형.

5. Gunawan, A., Lau, H. C., & Vansteenwegen, P. (2015). *A Survey on Algorithmic Approaches for Solving Tourist Trip Design Problems*. Journal of Heuristics.
   - TTDP에서 시간·이동·후보 축소·관광 일정 최적화의 배경.
   - DOI: 10.1007/s10732-014-9242-5

6. Efficient Itinerary Planning with Category Constraints (ACM GIS 2014).
   - 관광 일정 계획에 POI 카테고리 정보를 추가하고 카테고리 제약을 적용하는 접근.
   - https://www.cs.toronto.edu/~periklis/pubs/acmgis14.pdf

7. Recommending and Planning Trip Itineraries for Individual Travellers and Groups of Tourists (ICAPS 2016).
   - 사용자 관심·POI 인기도·시간 예산·이동시간·재방문 금지 등을 함께 다루는 일정 추천 모델.
   - https://people.sutd.edu.sg/~kwanhui_lim/publications/2016-ICAPS-tourRecDC.pdf

---

## 12. 구현자 최종 지시

1. 프로젝트 현재 코드와 이미 반영된 브랜드 쏠림 수정 코드를 먼저 읽고, 기존 `normalize_brand_key` 또는 동등 로직이 있으면 중복 구현하지 말고 개선한다.
2. 현재 ERD에는 브랜드 필드가 없으므로 DB 스키마 변경은 이번 작업의 필수 조건이 아니다.
3. 브랜드 제약만으로 끝내지 말고, `ctgno` 기반의 하루·전체 일정 카테고리 다양성 제약을 구현한다.
4. 기존 relevance를 수정하거나 거리 항목을 억지로 relevance에 합치지 않는다. `selection_score`와 alternatives의 `replacement_score`를 별도로 둔다.
5. `/recommend`와 `/alternatives`에서 공용 컨텍스트·제약·점수 함수를 호출하도록 리팩터링한다.
6. 정책 상수와 완화 조건을 코드 곳곳에 하드코딩하지 말고 `al02_policy.py` 등 중앙 정책 모듈에 둔다.
7. 변경 파일, 변경 이유, 테스트 목록·결과, 실제 DB/API 재현 결과, 남은 리스크를 완료 보고에 명시한다.
8. 구현 과정에서 현재 코드 구조나 실제 카테고리 ID가 본 문서의 가정과 다르면, 임의로 변경하지 말고 실제 스키마·데이터를 우선으로 맞추며 차이를 보고한다.
