"""
experts/signals 테이블에 전문가 경험칙 신호 정의를 등록하는 CLI.

멱등이다(원칙 6) — 몇 번을 다시 돌려도 (expert_id)/(signal_id) 기준으로
upsert만 하고 중복 행이 생기지 않는다. 신호 "정의"(이름·설명·방향·출처)만
여기서 등록하고, 실제 값(signal_values)은 scripts/compute_signals.py가
analysis/signals.py로 계산해서 채운다 — 정의와 계산을 분리해둔 이유는
새 신호를 추가하거나 설명 문구를 고칠 때 과거 계산값을 다시 돌릴 필요가
없게 하기 위해서다.

description은 전부 사용자가 영상 내용을 직접 요약해서 전달한 문구를 그대로
옮긴 것이다 — 원문을 인용한 게 아니라 "이런 취지로 말했다"는 요약이고,
괄호 안 문장은 그 취지를 한 줄로 압축한 캐치프레이즈다(원칙 5: 1인칭
발언을 지어내지 않는다. 이 파일은 발언을 재구성하지 않고 경험칙의 논리만
기계적으로 설명한다).

사용:
    python scripts/seed_signals.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402

EXPERTS = [
    {
        "expert_id": "KHJ",
        "name": "전문가 A",
        "title": "이코노미스트 (공개 방송 출연)",
        "source_show": "공개 경제 방송(출처 대응표는 비공개)",
        "note": "방송 A-1 (2026-07-13 촬영)",
    },
    {
        "expert_id": "YJH",
        "name": "전문가 B",
        "title": "평론가 (공개 방송 출연)",
        "source_show": "공개 경제 방송(출처 대응표는 비공개)",
        "note": "방송 B-1(2026-06-30 촬영), 방송 B-2(2026-08 촬영 추정)",
    },
    {
        "expert_id": "KSJ",
        "name": "전문가 C",
        "title": "투자자문 (공개 방송 출연)",
        "source_show": "공개 경제 방송(출처 대응표는 비공개)",
        "note": "방송 C-1 (2026-08 촬영 추정)",
    },
]

SIGNALS = [
    # --- 전문가 A ---
    {
        "signal_id": "KHJ_3DAY_SELLOFF", "name": "3일 투매 회피", "expert_id": "KHJ",
        "source_ref": "방송 A-1", "kind": "trade_rule", "direction": "opportunity",
        "data_source": "000660 일봉 종가", "is_manual": 0,
        "description": "000660이 2일 연속 하락(각 -3% 이하 또는 2일 합계 -8% 이하)이면 "
                        "그 다음 날 on. (\"셋째 날 투매에 동참하지 마라.\") 임계값 "
                        "-2/-3/-5% 민감도는 scripts/signal_scorecard.py에서 같이 비교한다.",
    },
    {
        "signal_id": "KHJ_CONCENTRATION", "name": "주변부 쏠림", "expert_id": "KHJ",
        "source_ref": "방송 A-1", "kind": "regime_filter", "direction": "risk",
        "data_source": "KOSDAQ·S&P500·다우·Euro Stoxx50·Sensex·Bovespa·VNM(베트남 ETF 대리)·"
                        "BIST100·SI=F(은) 63거래일 수익률",
        "is_manual": 0,
        "description": "주도 시장(코스피·나스닥·대만)은 플러스인데 주변부 9개 시장 중 "
                        "n_threshold(기본 5)개 이상이 63거래일 마이너스면 on.",
    },
    {
        "signal_id": "KHJ_OVERHEAT", "name": "과열", "expert_id": "KHJ",
        "source_ref": "방송 A-1", "kind": "regime_filter", "direction": "risk",
        "data_source": "000660 일봉 종가·거래량", "is_manual": 0,
        "description": "000660 거래량이 20일 평균의 2배 이상이거나 종가가 20일선보다 "
                        "15% 이상 높으면 on.",
    },
    {
        "signal_id": "KHJ_RATE_STAGE", "name": "금리 단계", "expert_id": "KHJ",
        "source_ref": "방송 A-1", "kind": "regime_filter", "direction": "risk",
        "data_source": "수동 입력(manual_inputs: rate_stage)", "is_manual": 1,
        "description": "미국 기준금리 단계(인상 전/1회/2~3회/이전 고점 도달)를 수동으로 "
                        "관리. \"이전 고점 도달\" 단계일 때만 on, 다른 단계는 대시보드에 "
                        "상태만 표시.",
    },
    {
        "signal_id": "KHJ_IPO_WEAK", "name": "IPO ETF 약세", "expert_id": "KHJ",
        "source_ref": "방송 A-1", "kind": "regime_filter", "direction": "risk",
        "data_source": "IPO ETF(IPOETF)·S&P500 63거래일 수익률", "is_manual": 0,
        "description": "IPO ETF 63일 수익률 - S&P500 63일 수익률이 기준(기본 -10%p) "
                        "이하면 on. (\"지수는 버티는데 IPO ETF가 힘이 없으면 시장이 소화를 "
                        "못 하는 중.\")",
    },
    {
        "signal_id": "KHJ_COMBO", "name": "복합 고점 신호", "expert_id": "KHJ",
        "source_ref": "방송 A-1", "kind": "regime_filter", "direction": "risk",
        "data_source": "KHJ_RATE_STAGE·KHJ_CONCENTRATION·KHJ_IPO_WEAK 동시 점등 개수",
        "is_manual": 0,
        "description": "세 신호 중 동시에 켜진 개수(0~3). 3개 모두 켜지면 분할 매도 "
                        "단계 트리거. (\"고점은 여러 신호가 동시에 올 때만, 고점 매도는 "
                        "운이니 3~5등분 기계적 분할 매도.\")",
    },
    # --- 전문가 B ---
    {
        "signal_id": "YJH_BOX_RANGE_BUY", "name": "박스권 하단 매수 후보", "expert_id": "YJH",
        "source_ref": "방송 B-1·B-2", "kind": "trade_rule", "direction": "opportunity",
        "data_source": "000660 일봉 종가, 60거래일 고저", "is_manual": 0,
        "description": "최근 60거래일 고저 범위 하단 20% 안이면 on. (\"박스권에서는 "
                        "반도체 밀리면 사고.\")",
    },
    {
        "signal_id": "YJH_BOX_RANGE_TRIM", "name": "박스권 상단 축소 후보", "expert_id": "YJH",
        "source_ref": "방송 B-1·B-2", "kind": "trade_rule", "direction": "risk",
        "data_source": "000660 일봉 종가, 60거래일 고저", "is_manual": 0,
        "description": "최근 60거래일 고저 범위 상단 20% 안이면 on. (\"오르면 줄여라.\")",
    },
    {
        "signal_id": "YJH_US10Y", "name": "미국 10년물 고금리", "expert_id": "YJH",
        "source_ref": "방송 B-1·B-2", "kind": "regime_filter", "direction": "risk",
        "data_source": "^TNX 종가", "is_manual": 0,
        "description": "미국 10년물이 4.5% 이상이면 on — 하이퍼스케일러 자금조달 비용 "
                        "상승 대리 지표.",
    },
    {
        "signal_id": "YJH_MEMORY_MARGIN", "name": "메모리 마진 하락 전환", "expert_id": "YJH",
        "source_ref": "방송 B-1·B-2", "kind": "regime_filter", "direction": "risk",
        "data_source": "수동 입력(manual_inputs: memory_margin, 마이크론 분기 매출총이익률)",
        "is_manual": 1,
        "description": "마이크론 매출총이익률이 80%대까지 올랐던 적이 있고 지금 60% "
                        "이하면 on. (\"마진이 80%대에서 50~60%로 내려올 때 시장이 "
                        "출렁인다.\")",
    },
    {
        "signal_id": "YJH_PBR_FLOOR", "name": "PBR 저점 근접", "expert_id": "YJH",
        "source_ref": "방송 B-1·B-2", "kind": "trade_rule", "direction": "opportunity",
        "data_source": "수동 입력(manual_inputs: pbr, 000660 선행 PBR)", "is_manual": 1,
        "description": "000660 선행 PBR이 역사적 하단(기본 1.1배 이하) 근처면 on. "
                        "(\"가치를 싸게 얻는 구간.\")",
    },
    # --- 전문가 C ---
    {
        "signal_id": "KSJ_PANIC_DAY", "name": "패닉 데이", "expert_id": "KSJ",
        "source_ref": "방송 C-1", "kind": "trade_rule", "direction": "opportunity",
        "data_source": "KOSPI 일봉 종가", "is_manual": 0,
        "description": "KOSPI가 하루 -5% 이상 하락한 다음 날 on. 서킷브레이커 데이터는 "
                        "구할 수 없어 일일 수익률 기준만 본다(확인 필요로 남김). "
                        "(\"아비규환 때 사라.\")",
    },
    {
        "signal_id": "KSJ_BREADTH", "name": "반도체 쏠림(매수 소진)", "expert_id": "KSJ",
        "source_ref": "방송 C-1", "kind": "regime_filter", "direction": "risk",
        "data_source": "KOSPI·005930·000660 20거래일 수익률 (반도체 2종목 가중평균으로 "
                        "'반도체 제외 KOSPI' 근사 — 실제 상승종목비율 데이터 아님, 확인 필요)",
        "is_manual": 0,
        "description": "KOSPI 20일 수익률은 플러스인데 반도체 2종목만 유독 많이 오르고 "
                        "있으면(근사 지표) on. (\"다른 종목을 팔아서 반도체를 사기 "
                        "시작하면 돈이 떨어져 가는 중. 사자가 끝나면 상승도 끝.\")",
    },
    {
        "signal_id": "KSJ_US10Y_HIGH", "name": "미국 10년물 고금리(강)", "expert_id": "KSJ",
        "source_ref": "방송 C-1", "kind": "regime_filter", "direction": "risk",
        "data_source": "^TNX 종가", "is_manual": 0,
        "description": "미국 10년물이 4.8% 이상이면 on. YJH_US10Y와 상관 높음 — "
                        "analysis/weighting.py가 가중치 중복을 줄인다. "
                        "(\"무위험 수익률이 5%면 위험자산은 8~10%를 내야 한다는 "
                        "의문이 생긴다.\")",
    },
    {
        "signal_id": "KSJ_FOREIGN_SELL", "name": "외국인 순매도", "expert_id": "KSJ",
        "source_ref": "방송 C-1", "kind": "regime_filter", "direction": "risk",
        "data_source": "KIS investor_flow (collectors/kis_client.py, 조회 전용)",
        "is_manual": 0, "weight_cap_pct": 15.0, "enabled": 1,
        "description": "외국인 20일 누적 순매도가 기준 이상이면 on. 2026-10-06 세션 4차에서 "
                        "analysis/signals.ksj_foreign_sell()·collectors/kis_client.investor_flow() "
                        "구현 완료 — 단, 이 세션은 KIS 키 없이 진행했으므로 실제 수급 데이터는 "
                        "아직 없다(수집 전까지 scripts/compute_signals.py가 '데이터 없음'으로 "
                        "건너뜀, 가중치는 자동으로 0). KIS 키 발급 후 collectors/kis_client.py로 "
                        "실제 수집하면 바로 계산이 시작된다. 전문가 A 지적대로(\"외국인 매도는 "
                        "비중 한도 같은 기계적 이유일 수 있으니 단독 판단 금지\") 가중치 상한을 "
                        "15%로 낮게 둔다.",
    },
    {
        "signal_id": "KSJ_DCA", "name": "분할 매수 포맷", "expert_id": "KSJ",
        "source_ref": "방송 C-1", "kind": "trade_rule", "direction": None,
        "data_source": "N/A — on/off 신호가 아니라 표시 방식 규칙", "is_manual": 0,
        "enabled": 0,
        "description": "매수 후보 신호가 나와도 한 번에 사지 말고 N회(기본 3회, 3거래일 "
                        "간격) 분할 매수로 표시하라는 규칙. 백테스트 대상 on/off 신호가 "
                        "아니라 대시보드 표시 포맷이라 enabled=0으로 등록만 해둠. "
                        "(\"타이밍 재지 말고 시간을 분할하라.\")",
    },
]


def main() -> None:
    store.init_db()
    for row in EXPERTS:
        store.upsert_expert(row)
    for row in SIGNALS:
        store.upsert_signal(row)
    print(f"등록됨: 전문가 {len(EXPERTS)}명, 신호 {len(SIGNALS)}개")
    for row in SIGNALS:
        status = "" if row.get("enabled", 1) else "  [미계산]"
        print(f"  {row['signal_id']:24s} {row['kind']:14s} {row['direction'] or '-':12s}{status}")


if __name__ == "__main__":
    main()
