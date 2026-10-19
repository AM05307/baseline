"""
executor — baseline과 분리된 실행 계층(자본 사다리·주문 계획·브로커 어댑터).

의존 방향: executor → (읽기 전용) analysis/·db/. baseline 쪽 코드는 executor를 import하지 않는다
(tests/test_executor_guards.py). 원칙과 안전장치는 executor/EXECUTOR.md.
"""
