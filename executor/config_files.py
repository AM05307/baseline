"""
설정 파일 로더 — 읽을 때마다 내용 해시를 executor.db config_history와 비교해서, 바뀌었으면 내용 전체를
append-only로 남긴다(사전 등록한 기준을 결과를 본 뒤 고쳤는지 나중에 확인할 수 있게).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from executor import store
from executor.krx_calendar import KrxCalendar, parse_calendar
from executor.tiers import Ladder, parse_ladder

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def resolve_config(name: str, config_dir: Path = CONFIG_DIR) -> Path:
    """
    설정 파일 고르기: <name>.local.yaml(실제 값, gitignore) → <name>.example.yaml(저장소의 예시값) → <name>.yaml.
    실제 운용 금액 같은 개인 값은 local에만 둔다(공개 저장소에는 example만).
    """
    for cand in (f"{name}.local.yaml", f"{name}.example.yaml", f"{name}.yaml"):
        p = Path(config_dir) / cand
        if p.exists():
            return p
    raise FileNotFoundError(f"설정 파일 없음: {config_dir}/{name}(.local|.example).yaml")


def is_example(path: Path) -> bool:
    """예시값 파일로 돌고 있는가 — 화면에 표시한다."""
    return Path(path).name.endswith(".example.yaml")


TIERS_PATH = CONFIG_DIR / "capital_tiers.example.yaml"  # 저장소에 있는 예시 파일(테스트·문서 기준)


def _load(file_path: Path, db_path: Path | None, record: bool) -> dict:
    text = Path(file_path).read_text(encoding="utf-8")
    if record:
        store.record_config_version(str(Path(file_path).name), text, path=db_path)
    return yaml.safe_load(text) or {}


def load_ladder(file_path: Path | None = None, db_path: Path | None = None, record: bool = True) -> Ladder:
    """record=False는 읽기 전용 화면용(DB에 아무것도 쓰지 않음). 스크립트·엔진은 기본값(기록).
    file_path가 없으면 resolve_config("capital_tiers") — local이 있으면 local, 없으면 example."""
    return parse_ladder(_load(file_path or resolve_config("capital_tiers"), db_path, record))


def load_criteria(file_path: Path | None = None, db_path: Path | None = None, record: bool = True) -> dict:
    return _load(file_path or resolve_config("promotion_criteria"), db_path, record)


def load_executor_config(file_path: Path | None = None, db_path: Path | None = None, record: bool = True) -> dict:
    cfg = _load(file_path or resolve_config("executor"), db_path, record)
    cfg.setdefault("benchmarks", ["000660"])
    cfg.setdefault("cap_rejected_streak", 3)
    cfg.setdefault("kill_switch_file", "KILL_SWITCH")
    cfg.setdefault("fill", "next_open")
    cfg.setdefault("max_weight_gap_pctp", 10)
    cfg.setdefault("daily_limits", {"max_daily_loss_pct": 3.0, "max_orders_per_day": 4, "cooldown_days": 5})
    cfg.setdefault("concentration", {"max_single_weight": 0.30, "exempt": ["069500", "SPY", "QQQ"]})
    if int(cfg["cap_rejected_streak"]) < 1:
        raise ValueError("cap_rejected_streak는 1 이상")
    return cfg


def resolve(path_str: str) -> Path:
    """설정 파일 안의 상대 경로는 저장소 루트 기준."""
    p = Path(path_str)
    return p if p.is_absolute() else ROOT / p


def load_calendar(file_path: Path | None = None, db_path: Path | None = None, record: bool = True) -> KrxCalendar:
    if file_path is None:
        file_path = resolve(load_executor_config(db_path=db_path, record=record).get("calendar_file",
                                                                                   "config/krx_holidays.yaml"))
    return parse_calendar(_load(file_path, db_path, record))
