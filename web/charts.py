"""
서버사이드 SVG 차트 생성.

JS 차트 라이브러리를 쓰지 않는다 — 의존성을 늘리고 싶지 않았고, 어차피
정적인 관측 대시보드라 인터랙티브 차트가 꼭 필요하지도 않다. pandas Series를
받아서 그리드라인 있는 <svg> 문자열을 돌려주는 순수 함수 모음.

web/ 패키지 소속이라 analysis/처럼 "DB·API 금지" 규칙을 지킬 필요는 없지만,
실제로 이 파일도 pandas Series만 받고 DB를 직접 조회하지 않는다 — 재사용성
때문에 그렇게 짰을 뿐이지 원칙 때문은 아니다.
"""

from __future__ import annotations

import pandas as pd

FONT = "-apple-system,BlinkMacSystemFont,'SF Pro Text','Apple SD Gothic Neo',sans-serif"


def _to_y(v: float, lo: float, hi: float, pad_t: float, plot_h: float) -> float:
    if hi == lo:
        return pad_t + plot_h / 2
    return pad_t + plot_h - (v - lo) / (hi - lo) * plot_h


def line_chart(
    series_list: list[pd.Series],
    colors: list[str],
    dashed: list[bool] | None = None,
    w: int = 760,
    h: int = 280,
    pad_l: int = 54,
    pad_b: int = 26,
    pad_t: int = 12,
    pad_r: int = 12,
    y_ticks: int = 5,
    x_tick_count: int = 4,
    value_fmt: str = "{:,.0f}",
) -> str:
    """
    series_list의 모든 시리즈를 같은 축에 겹쳐 그린다 (정규화 등은 호출부 책임).
    가로 보조선 + Y축 라벨, 세로 보조선, X축 날짜 라벨을 포함한다.
    빈 시리즈가 섞여 있으면 그 시리즈는 건너뛴다.
    """
    dashed = dashed or [False] * len(series_list)
    valid = [(s, c, d) for s, c, d in zip(series_list, colors, dashed) if not s.empty]
    if not valid:
        return _empty_chart(w, h, "표시할 데이터 없음")

    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    all_vals = pd.concat([s for s, _, _ in valid])
    lo, hi = float(all_vals.min()), float(all_vals.max())
    pad_v = (hi - lo) * 0.08 or (abs(hi) * 0.05 or 1)
    lo -= pad_v
    hi += pad_v

    n = max(len(s) for s, _, _ in valid)
    if n < 2:
        return _empty_chart(w, h, "표본 부족 (2개 미만)")

    svg = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none" '
           f'style="overflow:visible" role="img">']

    for i in range(y_ticks):
        v = lo + (hi - lo) * i / (y_ticks - 1)
        y = _to_y(v, lo, hi, pad_t, plot_h)
        svg.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+plot_w}" y2="{y:.1f}" '
                    f'stroke="#e5e5e7" stroke-width="1"/>')
        svg.append(f'<text x="{pad_l-10}" y="{y+3:.1f}" text-anchor="end" font-size="10.5" '
                    f'fill="#86868b" font-family="{FONT}">{value_fmt.format(v)}</text>')

    for i in range(x_tick_count + 1):
        x = pad_l + plot_w * i / x_tick_count
        svg.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t+plot_h}" '
                    f'stroke="#f0f0f2" stroke-width="1"/>')

    ref_idx = max(valid, key=lambda t: len(t[0]))[0].index
    labels = []
    for i in range(x_tick_count + 1):
        pos = min(int(len(ref_idx) * i / x_tick_count), len(ref_idx) - 1)
        labels.append(ref_idx[pos])
    for i, lab in enumerate(labels):
        x = pad_l + plot_w * i / x_tick_count
        anchor = "start" if i == 0 else ("end" if i == len(labels) - 1 else "middle")
        text = lab.strftime("%m-%d") if hasattr(lab, "strftime") else str(lab)
        svg.append(f'<text x="{x:.1f}" y="{h-6}" text-anchor="{anchor}" font-size="10.5" '
                    f'fill="#86868b" font-family="{FONT}">{text}</text>')

    for s, color, dash in valid:
        xs = [pad_l + plot_w * i / (len(s) - 1 if len(s) > 1 else 1) for i in range(len(s))]
        pts = " ".join(f"{x:.1f},{_to_y(v, lo, hi, pad_t, plot_h):.1f}" for x, v in zip(xs, s.values))
        dash_attr = ' stroke-dasharray="4 4"' if dash else ""
        svg.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8"'
                    f'{dash_attr} stroke-linejoin="round" stroke-linecap="round"/>')
        lx, ly = xs[-1], _to_y(s.values[-1], lo, hi, pad_t, plot_h)
        svg.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="{color}"/>')

    svg.append("</svg>")
    return "\n".join(svg)


def sparkline(series: pd.Series, up: bool, w: int = 170, h: int = 50) -> str:
    """작은 다중 패널용 미니 차트 — 보조선 최소화, 등락 색만."""
    color = "#248a3d" if up else "#d70015"
    return line_chart([series], [color], w=w, h=h, pad_l=34, pad_b=16, pad_t=6,
                       y_ticks=3, x_tick_count=1)


def gauge_svg(score: float, w: int = 230, h: int = 140) -> str:
    """
    0~100 점수를 반원 게이지로 그린다 (analysis/sentiment.py의 SentimentGauge.score).
    5개 고정 색상 밴드(공포→탐욕)는 정보 전달용 관례색이라 프로젝트의
    "색은 최소화" 방향과는 별개로 유지한다 — 이 차트 유형 자체가 원래 그렇게 읽힌다.
    """
    import math

    cx, cy, r = w / 2, h - 8, min(w / 2 - 10, h - 30)
    bands = [(180, 144, "#d70015"), (144, 108, "#ff9500"), (108, 72, "#ffd60a"),
             (72, 36, "#8bc34a"), (36, 0, "#248a3d")]

    def pt(deg: float, radius: float) -> tuple[float, float]:
        rad = math.radians(deg)
        return cx - radius * math.cos(rad), cy - radius * math.sin(rad)

    svg = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img">']
    for a0, a1, color in bands:
        x0, y0 = pt(a0, r)
        x1, y1 = pt(a1, r)
        svg.append(f'<path d="M {x0:.1f} {y0:.1f} A {r} {r} 0 0 1 {x1:.1f} {y1:.1f}" '
                    f'stroke="{color}" stroke-width="14" fill="none" stroke-linecap="round"/>')

    score = max(0.0, min(100.0, score))
    needle_deg = 180 - (score / 100 * 180)
    nx, ny = pt(needle_deg, r - 24)
    svg.append(f'<line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" '
                f'stroke="#1d1d1f" stroke-width="2.5" stroke-linecap="round"/>')
    svg.append(f'<circle cx="{cx}" cy="{cy}" r="3.5" fill="#1d1d1f"/>')
    svg.append(f'<text x="{cx}" y="{cy-16}" text-anchor="middle" font-size="22" font-weight="600" '
                f'fill="#1d1d1f" font-family="{FONT}">{score:.0f}</text>')
    svg.append("</svg>")
    return "\n".join(svg)


def _empty_chart(w: int, h: int, message: str) -> str:
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">'
        f'<text x="{w/2}" y="{h/2}" text-anchor="middle" font-size="12.5" fill="#86868b" '
        f'font-family="{FONT}">{message}</text></svg>'
    )
