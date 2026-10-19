"""설정. 비밀값은 .env 에서만 읽는다. 절대 커밋하지 않는다."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def _load_env() -> None:
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

_load_env()

@dataclass(frozen=True)
class Settings:
    kis_app_key: str = os.getenv("KIS_APP_KEY", "")
    kis_app_secret: str = os.getenv("KIS_APP_SECRET", "")
    kis_account: str = os.getenv("KIS_ACCOUNT", "")
    use_paper: bool = os.getenv("KIS_USE_PAPER", "true").lower() == "true"
    watch_kr: str = os.getenv("WATCH_KR", "000660")
    # SK하이닉스 ADR(SKHY) 전환비율(1 ADS = 보통주 1/ratio주). 공식 예탁계약서
    # (F-1/depositary agreement)로 재확인 전까지 10을 임시 기본값으로 쓴다
    # (analysis/adr.py 모듈 docstring 참고) — 재확인 후 값이 다르면 .env의
    # ADR_RATIO만 바꾸면 된다(코드 수정 불필요).
    adr_ratio: int = int(os.getenv("ADR_RATIO") or "10")  # 빈 문자열(.env.example 기본)도 10으로 처리
    # OpenDART(전자공시) API 키. https://opendart.fss.or.kr 에서 무료 발급.
    # collectors/dart_client.py — 키 없으면 네트워크를 전혀 시도하지 않는다.
    dart_api_key: str = os.getenv("DART_API_KEY", "")

    @property
    def kis_ready(self) -> bool:
        return bool(self.kis_app_key and self.kis_app_secret)

    @property
    def dart_ready(self) -> bool:
        return bool(self.dart_api_key)

settings = Settings()
