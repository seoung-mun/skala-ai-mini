"""질문과 답변이 공유하는 아라비아 숫자·한글 금액 단위 파서."""

import re
from decimal import Decimal

NUMBER = r"\d[\d,]*(?:\.\d+)?"
SMALL = rf"(?:(?:{NUMBER}\s*)?[천백십]\s*)+"
COEFFICIENT = rf"(?:{SMALL}(?:{NUMBER})?|{NUMBER})"
MAJOR = rf"{COEFFICIENT}\s*[조억만]\s*"
MONEY_RE = re.compile(
    rf"(?<![\d.,])[+−-]?(?:(?:{MAJOR})+(?:{COEFFICIENT}\s*원|{SMALL}원?)?원?|{COEFFICIENT}\s*원)"
)
_MAJOR = re.compile(rf"({COEFFICIENT})\s*([조억만])\s*")
_SMALL = re.compile(rf"(?:({NUMBER})\s*)?([천백십])|({NUMBER})")
UNITS = {
    "조": 10**12,
    "억": 10**8,
    "만": 10**4,
    "천": 1000,
    "백": 100,
    "십": 10,
}


def _coefficient(text: str) -> Decimal:
    return sum(
        (
            Decimal((number or "1").replace(",", "")) * UNITS[unit]
            if unit
            else Decimal(bare.replace(",", ""))
        )
        for number, unit, bare in _SMALL.findall(text)
    )


def money_values(text: str) -> list[tuple[int, Decimal]]:
    """복합 금액을 한 수치로 읽는다. 위치는 원래 텍스트 기준이다."""
    values = []
    for match in MONEY_RE.finditer(text):
        raw = match[0].strip()
        if not raw.endswith("원") and re.match(
            r"\s*(?:명|개|년|월|일|세|점|배|%|퍼센트)", text[match.end() :]
        ):
            continue
        sign = -1 if raw.startswith(("-", "−")) else 1
        body = raw.lstrip("+−-").removesuffix("원").strip()
        total, end, last_unit = Decimal(0), 0, 1
        for part in _MAJOR.finditer(body):
            last_unit = UNITS[part[2]]
            total += _coefficient(part[1]) * last_unit
            end = part.end()
        tail = body[end:].strip()
        if tail:
            # shortcut: 원이 생략된 '1억5천'은 '1억5천만원'. '1억5천원'은 문자 그대로 읽는다.
            factor = (
                last_unit // 10_000
                if last_unit >= 10**8 and not raw.endswith("원")
                else 1
            )
            total += _coefficient(tail) * factor
        values.append((match.start(), sign * total))
    return values
