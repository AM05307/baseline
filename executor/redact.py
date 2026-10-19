"""
비밀값 마스킹 — 키·시크릿·토큰·계좌번호가 로그·예외·DB·테스트 출력에 그대로 나가지 않게 한다.

mask(): 화면에 "설정됐는지"만 보일 정도로 줄인다(앞 2글자 + ****). 빈 값은 "(없음)".
Redactor.redact(): 문자열 안에 등록된 비밀값이 들어 있으면 전부 [마스킹]으로 바꾼다 — 예외 메시지·응답 본문을
남기기 전에 반드시 거친다.
"""

from __future__ import annotations

MASK = "[마스킹]"


def mask(value: str | None) -> str:
    if not value:
        return "(없음)"
    return value[:2] + "****"


class Redactor:
    def __init__(self, secrets: list[str | None] | None = None):
        self._secrets: list[str] = []
        for s in secrets or []:
            self.add(s)

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= 4 and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)  # 긴 값부터 — 부분 문자열 겹침 방지

    def redact(self, text: object) -> str:
        out = str(text)
        for s in self._secrets:
            out = out.replace(s, MASK)
        return out
