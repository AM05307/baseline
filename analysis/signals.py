"""
전문가 경험칙 신호 — 순수 함수.

각 함수는 "전문가가 영상에서 말한 경험칙"을 기계적으로 계산만 한다. 1인칭
발언을 재구성하지 않고(원칙 5), 가격을 예측하지도 않는다(원칙 1) — 과거/
현재 데이터로 계산 가능한 조건을 on/off(+수치)로 바꾸는 것뿐이다. 이
신호들이 실제로 쓸모 있는지는 analysis/signal_backtest.py가 과거 데이터로
검증하고, 그 성적만큼만 analysis/weighting.py가 가중치를 준다 — 신호
이름이나 "전문가스러움"이 가중치를 정하지 않는다.

전부 DB/API를 모른다. 날짜 인덱스가 있는 pandas Series/DataFrame만 받는다.
미래 참조 금지: 모든 함수는 그 날짜까지의 데이터로만 그 날짜의 값을 낸다
(rolling/shift류만 사용). "오늘 신호가 켜졌다"를 실제 매매에 반영하는
시점(체결 지연)은 이 모듈이 책임지지 않는다 — analysis/signal_backtest.py가
rule_backtest.py·pullback.py와 같은 "신호 다음날 반영" 원칙을 적용한다.

파라미터 기본값은 사용자가 지정한 값(예: -3%/-8%, 4.5%, 4.8%)을 그대로
쓰거나, 명시되지 않은 곳은 합리적인 시작값을 썼다(과최적화 방지 목적 —
analysis/strategies.py와 같은 이유로 데이터에 맞춰 고르지 않았다). 그렇게
표시해둔 파라미터는 코드 주석에 "기본값, 백테스트로 검증 전"이라고 남긴다.
"""

from __future__ import annotations

import pandas as pd


# ---------------------------------------------------------------------------
# 전문가 A (KHJ)
# ---------------------------------------------------------------------------

def khj_3day_selloff(
    close: pd.Series, day_pct: float = -3.0, two_day_pct: float = -8.0
) -> pd.DataFrame:
    """
    "셋째 날 투매에 동참하지 마라": 2일 연속 하락이고(둘 다 음수), 그게
    (각 day_pct% 이하) 또는 (2일 합계 two_day_pct% 이하)면, 그 다음 날 on.

    day_pct/two_day_pct는 민감도 분석용으로 그대로 함수 인자다 — 기본
    -3%/-8%는 사용자가 지정한 값. -2/-5% 등으로 바꿔 불러서 민감도를
    비교할 수 있다(analysis/signal_backtest.py 쪽 스윕에서 그렇게 쓴다).
    """
    close = close.sort_index()
    day_ret = close.pct_change() * 100
    prev_ret = day_ret.shift(1)
    two_day_ret = (close / close.shift(2) - 1) * 100

    both_down = (day_ret < 0) & (prev_ret < 0)
    hard_each = (day_ret <= day_pct) & (prev_ret <= day_pct)
    hard_total = two_day_ret <= two_day_pct
    selloff_day = both_down & (hard_each | hard_total)

    is_on = selloff_day.shift(1).fillna(False).astype(int)  # "다음 날" 반영
    value = two_day_ret.shift(1)
    return pd.DataFrame({"value": value, "is_on": is_on})


def khj_concentration(
    market_closes: dict[str, pd.Series], lookback: int = 63, n_threshold: int = 5
) -> pd.DataFrame:
    """
    "주도 시장(코스피·나스닥·대만)은 플러스인데 주변부가 마이너스가 많을수록
    위험": market_closes에 담긴 주변부 시장들의 lookback일 수익률이 마이너스인
    시장 수를 세고, n_threshold개 이상이면 on.

    n_threshold=5는 시작 기본값이다(전체 9개 시장 중 과반을 약간 넘는 수) —
    "N은 백테스트로 정하고 기록"이라고 하셨는데, 백테스트 데이터로 거꾸로
    끼워맞추면 과최적화라서 숫자 자체는 내가 고르지 않고 중립적인 기본값만
    뒀다. analysis/signal_backtest.py의 스코어카드에서 3~7 범위로 돌려본
    결과를 같이 보여주는 식으로 "기록"한다(파라미터를 데이터로 고르는 게
    아니라, 이미 고른 기본값이 민감도에 얼마나 강한지 투명하게 보여주는
    용도).
    """
    returns = {}
    for name, s in market_closes.items():
        s = s.sort_index()
        returns[name] = (s / s.shift(lookback) - 1) * 100
    df = pd.DataFrame(returns)
    neg_count = (df < 0).sum(axis=1)
    # 전부 NaN인 날(데이터 부족)은 신호 없음으로 — 일부만 있어도 있는 것만으로 센다.
    all_nan = df.isna().all(axis=1)
    is_on = ((neg_count >= n_threshold) & ~all_nan).astype(int)
    return pd.DataFrame({"value": neg_count.where(~all_nan), "is_on": is_on})


def khj_overheat(
    close: pd.Series, volume: pd.Series, ma_window: int = 20,
    volume_multiple: float = 2.0, price_premium_pct: float = 15.0,
) -> pd.DataFrame:
    """000660 거래량이 20일 평균의 2배 이상이거나, 종가가 20일선보다 15% 이상 높으면 on."""
    close, volume = close.sort_index(), volume.sort_index()
    vol_ma = volume.rolling(ma_window).mean()
    price_ma = close.rolling(ma_window).mean()
    vol_spike = volume >= vol_ma * volume_multiple
    price_spike = close >= price_ma * (1 + price_premium_pct / 100)
    is_on = (vol_spike | price_spike).fillna(False).astype(int)
    premium_pct = (close / price_ma - 1) * 100
    return pd.DataFrame({"value": premium_pct, "is_on": is_on})


RATE_STAGES = ["인상 전", "1회", "2~3회", "이전 고점 도달"]


def khj_rate_stage(stage_timeline: pd.Series) -> pd.DataFrame:
    """
    stage_timeline: db.store.get_manual_timeline("rate_stage")를 날짜
    인덱스에 forward-fill한 결과(수동 입력, RATE_STAGES 중 하나). "이전 고점
    도달" 단계일 때만 on — 나머지 단계는 대시보드에 상태만 표시하고 가중치
    계산에는 안 들어간다(규칙 그대로).
    """
    stage_timeline = stage_timeline.sort_index()
    is_on = (stage_timeline == "이전 고점 도달").fillna(False).astype(int)
    return pd.DataFrame({"value": stage_timeline, "is_on": is_on})


def khj_ipo_weak(
    ipo_etf_close: pd.Series, benchmark_close: pd.Series,
    lookback: int = 63, gap_threshold_pct: float = -10.0,
) -> pd.DataFrame:
    """
    "지수는 버티는데 IPO ETF가 힘이 없으면 시장이 소화를 못 하는 중":
    IPO ETF lookback일 수익률 - 벤치마크(S&P500) lookback일 수익률이
    gap_threshold_pct 이하면 on. gap_threshold_pct=-10은 시작 기본값(과최적화
    방지, 위 khj_concentration 설명과 같은 이유).
    """
    ipo, bench = ipo_etf_close.sort_index(), benchmark_close.sort_index()
    ipo_ret = (ipo / ipo.shift(lookback) - 1) * 100
    bench_ret = (bench / bench.shift(lookback) - 1) * 100
    gap = ipo_ret - bench_ret
    is_on = (gap <= gap_threshold_pct).fillna(False).astype(int)
    return pd.DataFrame({"value": gap, "is_on": is_on})


def khj_combo(rate_on: pd.Series, concentration_on: pd.Series, ipo_weak_on: pd.Series) -> pd.DataFrame:
    """
    RATE_STAGE(이전 고점 도달)·CONCENTRATION·IPO_WEAK 중 동시에 켜진 개수.
    3개 모두 켜지면 "분할 매도 단계"(대시보드/상태 엔진이 표시 — 이 함수는
    개수만 센다). "고점은 여러 신호가 동시에 올 때만."
    """
    df = pd.concat(
        [rate_on.fillna(0), concentration_on.fillna(0), ipo_weak_on.fillna(0)], axis=1,
    ).fillna(0)
    count = df.sum(axis=1)
    is_on = (count >= 1).astype(int)
    return pd.DataFrame({"value": count, "is_on": is_on})


# ---------------------------------------------------------------------------
# 전문가 B (YJH)
# ---------------------------------------------------------------------------

def yjh_box_range(
    close: pd.Series, lookback: int = 60, band_pct: float = 20.0,
) -> pd.DataFrame:
    """
    "박스권에서는 반도체 밀리면 사고 오르면 줄여라": 최근 lookback거래일
    고저 범위에서 하단 band_pct% 안이면 buy_candidate, 상단 band_pct% 안이면
    trim_candidate. value는 범위 내 위치(0=최저,100=최고, %).

    반환 DataFrame 컬럼: value(위치 %), is_on_buy, is_on_trim.
    (한 규칙에서 두 방향 후보가 나와서 analysis/signal_backtest.py 쪽에서
    YJH_BOX_RANGE_BUY(opportunity)·YJH_BOX_RANGE_TRIM(risk, "줄여라"는
    경계 신호로 분류) 두 개의 signal_id로 각각 기록한다.)
    """
    close = close.sort_index()
    roll_max = close.rolling(lookback).max()
    roll_min = close.rolling(lookback).min()
    rng = roll_max - roll_min
    position_pct = ((close - roll_min) / rng * 100).where(rng > 0)
    is_on_buy = (position_pct <= band_pct).fillna(False).astype(int)
    is_on_trim = (position_pct >= 100 - band_pct).fillna(False).astype(int)
    return pd.DataFrame({"value": position_pct, "is_on_buy": is_on_buy, "is_on_trim": is_on_trim})


def yjh_us10y(tnx_close: pd.Series, threshold_pct: float = 4.5) -> pd.DataFrame:
    """미국 10년물(^TNX)이 threshold_pct% 이상이면 on. ^TNX 종가는 이미 %값 그대로다."""
    tnx = tnx_close.sort_index()
    is_on = (tnx >= threshold_pct).fillna(False).astype(int)
    return pd.DataFrame({"value": tnx, "is_on": is_on})


def yjh_memory_margin(margin_timeline: pd.Series, peak_pct: float = 80.0, trough_pct: float = 60.0) -> pd.DataFrame:
    """
    margin_timeline: db.store.get_manual_timeline("memory_margin")을 날짜
    인덱스에 forward-fill한 마이크론 분기 매출총이익률(%). "80%대에서 60%
    이하로 내려오는 구간"을 과거 최고치가 peak_pct 이상이었다가 현재
    trough_pct 이하로 내려온 상태로 정의한다(절대 수준이 아니라 하락
    전환 구간).
    """
    margin_timeline = margin_timeline.sort_index()
    ever_peaked = (margin_timeline.cummax() >= peak_pct)  # 그 시점까지 한 번이라도 80%대 도달
    is_on = (ever_peaked & (margin_timeline <= trough_pct)).fillna(False).astype(int)
    return pd.DataFrame({"value": margin_timeline, "is_on": is_on})


def yjh_pbr_floor(pbr_timeline: pd.Series, floor_threshold: float = 1.1) -> pd.DataFrame:
    """
    pbr_timeline: db.store.get_manual_timeline("pbr")을 forward-fill한 000660
    선행 PBR. 역사적 하단(약 1배) 근처, floor_threshold=1.1 이하면 on.
    """
    pbr_timeline = pbr_timeline.sort_index()
    is_on = (pbr_timeline <= floor_threshold).fillna(False).astype(int)
    return pd.DataFrame({"value": pbr_timeline, "is_on": is_on})


# ---------------------------------------------------------------------------
# 전문가 C (KSJ)
# ---------------------------------------------------------------------------

def ksj_panic_day(kospi_close: pd.Series, threshold_pct: float = -5.0) -> pd.DataFrame:
    """
    "아비규환 때 사라": KOSPI가 하루 threshold_pct% 이상 하락한 다음 날 on.
    서킷브레이커 발동 여부는 별도 데이터 소스가 필요해 이번엔 구현하지 않음
    (yfinance엔 없음, 확인 필요로 남김) — 일일 수익률 기준만 본다.
    """
    kospi = kospi_close.sort_index()
    day_ret = kospi.pct_change() * 100
    panic_day = day_ret <= threshold_pct
    is_on = panic_day.shift(1).fillna(False).astype(int)
    return pd.DataFrame({"value": day_ret.shift(1), "is_on": is_on})


def ksj_breadth(
    kospi_close: pd.Series, samsung_close: pd.Series, hynix_close: pd.Series,
    lookback: int = 20, samsung_weight: float = 0.5,
) -> pd.DataFrame:
    """
    "다른 종목을 팔아서 반도체를 사기 시작하면 돈이 떨어져 가는 중": KOSPI
    lookback일 수익률은 플러스인데, 반도체 2종목을 뺀 나머지는 마이너스이면
    on.

    **데이터 제약(확인 필요)**: yfinance로는 KOSPI 상승종목비율이나
    "반도체 제외 KOSPI" 지수를 직접 구할 수 없다. 대신 반도체 2종목의
    가중평균 수익률(samsung_weight:1-samsung_weight)이 KOSPI 수익률보다
    훨씬 높은지로 "반도체만 오르고 있다"를 근사한다 — 실제 상승종목비율과
    다를 수 있다. KIS 수급 데이터나 KOSPI 구성종목 데이터가 생기면 더
    정확하게 교체할 것.
    """
    kospi, sec, hynix = kospi_close.sort_index(), samsung_close.sort_index(), hynix_close.sort_index()
    # 세 시리즈의 날짜 범위가 다를 수 있어서(수집 시작일이 제각각) 공통 날짜로
    # 먼저 맞춘다 — 안 그러면 뒤에서 인덱스가 다른 Series끼리 비교 연산을
    # 하다가 에러가 난다(pandas는 >, < 비교에 서로 다른 인덱스를 허용 안 함).
    common_idx = kospi.index.intersection(sec.index).intersection(hynix.index)
    kospi, sec, hynix = kospi.loc[common_idx], sec.loc[common_idx], hynix.loc[common_idx]
    kospi_ret = (kospi / kospi.shift(lookback) - 1) * 100
    sec_ret = (sec / sec.shift(lookback) - 1) * 100
    hynix_ret = (hynix / hynix.shift(lookback) - 1) * 100
    semis_ret = samsung_weight * sec_ret + (1 - samsung_weight) * hynix_ret
    gap = semis_ret - kospi_ret  # 반도체가 지수보다 얼마나 더 많이 올랐는지
    is_on = ((kospi_ret > 0) & (gap > 0) & (semis_ret > kospi_ret * 2)).fillna(False).astype(int)
    return pd.DataFrame({"value": gap, "is_on": is_on})


def ksj_us10y_high(tnx_close: pd.Series, threshold_pct: float = 4.8) -> pd.DataFrame:
    """미국 10년물이 threshold_pct% 이상이면 on. YJH_US10Y와 상관 높음 — weighting.py에서 처리."""
    tnx = tnx_close.sort_index()
    is_on = (tnx >= threshold_pct).fillna(False).astype(int)
    return pd.DataFrame({"value": tnx, "is_on": is_on})


def ksj_foreign_sell(
    foreign_net_qty: pd.Series, window: int = 20, sell_threshold: float = 0.0
) -> pd.DataFrame:
    """
    "외국인 20일 누적 순매도" 기준(4단계: KIS 키 없이 미리 구현).

    foreign_net_qty는 하루 단위 외국인 순매수 수량(양수=순매수, 음수=순매도)
    시계열 — collectors/kis_client.py의 investor_flow()가 db.store.
    upsert_flow()에 저장한 'foreigner' 컬럼을 scripts/compute_signals.py가
    그대로 읽어서 넘겨준다고 가정한다. **단위(수량 vs 금액)는 KIS 공식문서로
    아직 재확인하지 못했다**(collectors/kis_client.py의 TODO 참고) — 이
    함수는 부호(양/음)와 window일 합산만 쓰므로 단위가 무엇이든 "순매도
    누적" 판정 자체는 동작하지만, 실제 연결 시 반드시 함께 재확인할 것.

    window일 누적 순매수가 sell_threshold(기본 0, 즉 누적 순매도) 이하면
    다음 날 on. window=20은 "20일 누적"이라는 경험칙 설명을 그대로 따른
    시작 기본값이다(백테스트로 고른 값 아님 — 다른 신호들과 같은 과최적화
    방지 태도, 모듈 상단 docstring 참고). 미래 참조 금지(rolling+shift(1)).

    데이터가 없으면(원본 Series가 비어 있으면) 이 함수를 호출하는 쪽
    (scripts/compute_signals.py)이 호출 자체를 건너뛰고 "데이터 없음"으로
    표시한다 — 이 함수가 빈 입력을 받으면 빈 결과를 그대로 반환한다
    (지어내지 않음).
    """
    s = foreign_net_qty.sort_index()
    if s.empty:
        return pd.DataFrame({"value": pd.Series(dtype=float), "is_on": pd.Series(dtype=int)})
    cum = s.rolling(window).sum()
    condition = cum <= sell_threshold
    is_on = condition.shift(1).fillna(False).astype(int)
    value = cum.shift(1)
    return pd.DataFrame({"value": value, "is_on": is_on})
