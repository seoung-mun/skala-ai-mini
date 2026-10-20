"""금감원 금융상품한눈에 원본 JSON -> Product -> 마크다운.

마크다운은 섹션마다 검색 방식을 표시한다.
  <!-- retrieval: keyword -->   키워드 매칭·구조화 조회 대상 (이름·가입방법·금리표·데이터 주의)
  <!-- retrieval: embedding --> 임베딩 대상 (우대조건·수수료 같은 자유 텍스트)
베이스라인은 표시와 무관하게 모든 섹션을 키워드 매칭에 쓰고, 개선 단계에서 embedding 섹션만 벡터 색인한다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ENDPOINT_KIND = {
    "depositProductsSearch": "deposit",
    "savingProductsSearch": "saving",
    "mortgageLoanProductsSearch": "mortgage",
    "rentHouseLoanProductsSearch": "rent",
    "creditLoanProductsSearch": "credit",
}
KIND_KO = {"deposit": "정기예금", "saving": "적금", "mortgage": "주택담보대출",
           "rent": "전세자금대출", "credit": "개인신용대출"}
GRADES = [("crdt_grad_1", "900점 초과"), ("crdt_grad_4", "801~900점"), ("crdt_grad_5", "701~800점"),
          ("crdt_grad_6", "601~700점"), ("crdt_grad_10", "501~600점"), ("crdt_grad_11", "401~500점"),
          ("crdt_grad_12", "301~400점"), ("crdt_grad_13", "300점 이하")]
GROUP_KO = {"bank": "은행", "savingsbank": "저축은행"}
# 금감원 명세 기준 코드값. 실데이터에는 1·3만 등장했고 명세 원문과는 대조하지 않았다.
JOIN_DENY = {"1": "제한없음", "2": "서민전용", "3": "일부제한"}

DEPOSIT_TEXT = [("spcl_cnd", "우대조건"), ("join_member", "가입 대상"),
                ("mtrt_int", "만기 후 이자율"), ("etc_note", "기타 유의사항")]
LOAN_TEXT = [("loan_inci_expn", "부대비용"), ("erly_rpay_fee", "중도상환수수료"),
             ("dly_rate", "연체이자율"), ("loan_lmt", "대출 한도")]
EMBED_FIELDS = {"deposit": DEPOSIT_TEXT, "saving": DEPOSIT_TEXT,
                "mortgage": LOAN_TEXT, "rent": LOAN_TEXT, "credit": []}  # 신용대출은 텍스트 필드가 없다

NO_END_DAY = "99991231"  # 실데이터에서 종료일 없음 자리표시로 보이는 값(26건, 명세 미대조)
IDENTITY_TOL = 0.011  # 소수 2자리 반올림 오차 허용


@dataclass(frozen=True)
class Product:
    kind: str
    group: str  # bank | savingsbank
    key: tuple[str, ...]
    base: dict
    options: list[dict]
    flags: list[str]

    @property
    def id(self) -> str:
        # 같은 상품코드를 주담대·전세대출(예금·적금)이 함께 쓰는 경우가 있어 종류를 앞에 붙인다.
        return re.sub(r"[^0-9A-Za-z_-]", "-", "_".join((self.kind,) + self.key))

    @property
    def bank(self) -> str:
        return self.base["kor_co_nm"]

    @property
    def name(self) -> str:
        return self.base["fin_prdt_nm"]


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    return float(v)  # 해석 불가 값은 ValueError로 즉시 실패


def _text(v) -> str:
    if v is None:
        return ""
    lines = (re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in str(v).replace("\r", "").split("\n"))
    return "\n".join(ln for ln in lines if ln)


def _key(kind: str, row: dict) -> tuple[str, ...]:
    k = (row["dcls_month"], row["fin_co_no"], row["fin_prdt_cd"])
    # 신용대출은 같은 코드를 마이너스한도대출·카드론이 함께 쓰므로 대출종류까지 있어야 유일하다.
    return k + (row["crdt_prdt_type"],) if kind == "credit" else k


def _option(kind: str, row: dict) -> dict:
    if kind in ("saving", "deposit"):
        o = {"rate_type_nm": row["intr_rate_type_nm"], "term_months": int(row["save_trm"]),
             "rate": _num(row["intr_rate"]), "rate_max": _num(row["intr_rate2"])}
        if kind == "saving":
            o["rsrv_type_nm"] = row["rsrv_type_nm"]
        return o
    if kind in ("mortgage", "rent"):
        o = {"rpay_type_nm": row["rpay_type_nm"], "lend_rate_type_nm": row["lend_rate_type_nm"],
             "min": _num(row["lend_rate_min"]), "max": _num(row["lend_rate_max"]), "avg": _num(row["lend_rate_avg"])}
        if kind == "mortgage":
            o["mrtg_type_nm"] = row["mrtg_type_nm"]
        return o
    return {"type": row["crdt_lend_rate_type"], "type_nm": row["crdt_lend_rate_type_nm"],
            "grades": {c: _num(row[c]) for c, _ in GRADES}, "avg": _num(row["crdt_grad_avg"])}


def _flags(kind: str, options: list[dict]) -> list[str]:
    if not options:
        return ["금리 옵션 없음"]
    out: list[str] = []
    if kind in ("mortgage", "rent"):
        for o in options:
            lab = " ".join(filter(None, (o.get("mrtg_type_nm"), o["rpay_type_nm"], o["lend_rate_type_nm"])))
            lo, hi, av = o["min"], o["max"], o["avg"]
            if lo is None or hi is None:
                out.append(f"[{lab}] 최소/최대 금리 결측")
            elif lo > hi:
                out.append(f"[{lab}] 최소금리({lo})가 최대금리({hi})보다 큼")
            if av is None:
                out.append(f"[{lab}] 평균금리 결측")
            elif lo is not None and hi is not None and not lo <= av <= hi:
                out.append(f"[{lab}] 평균금리({av})가 최소~최대({lo}~{hi}) 범위 밖")
    elif kind == "credit":
        by = {o["type"]: o for o in options}
        if all(t in by for t in "ABCD"):  # A 대출금리 = B 기준금리 + C 가산금리 - D 가감조정금리
            for code, lab in GRADES:
                a, b, c, d = (by[t]["grades"][code] for t in "ABCD")
                if None not in (a, b, c, d) and abs(a - (b + c - d)) > IDENTITY_TOL:
                    out.append(f"[{lab}] 대출금리({a}) ≠ 기준({b}) + 가산({c}) − 가감({d})")
    return out


def parse_rows(kind: str, group: str, base_list: list[dict], option_list: list[dict]) -> list[Product]:
    bases: dict[tuple, dict] = {}
    for row in base_list:
        k = _key(kind, row)
        if k in bases:
            raise ValueError(f"{kind}/{group}: 상품 키 중복 {k}")
        bases[k] = {a: (_text(v) if isinstance(v, str) else v) for a, v in row.items()}
        for a in ("kor_co_nm", "fin_prdt_nm"):  # 이름에 줄바꿈이 섞인 상품이 있어 제목·표가 깨진다
            bases[k][a] = bases[k][a].replace("\n", " ")
        end = _text(row.get("dcls_end_day"))
        bases[k]["dcls_end_day"] = None if end in ("", NO_END_DAY) else end
    opts: dict[tuple, list[dict]] = {k: [] for k in bases}
    for row in option_list:
        k = _key(kind, row)
        if k not in opts:
            raise ValueError(f"{kind}/{group}: 상품 정보 없는 옵션 {k}")
        opts[k].append(_option(kind, row))
    return [Product(kind, group, k, bases[k], opts[k], _flags(kind, opts[k])) for k in bases]


def parse_file(path: Path) -> list[Product]:
    endpoint, _, group = path.stem.rpartition("_")
    d = json.loads(path.read_text())
    return parse_rows(ENDPOINT_KIND[endpoint], group, d["baseList"], d["optionList"])


def parse_all(raw_dir: Path) -> list[Product]:
    files = sorted(raw_dir.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"{raw_dir}에 원본 JSON이 없습니다")
    return [p for f in files for p in parse_file(f)]


# ---------- 마크다운 ----------
def _cell(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v).replace("|", "\\|").replace("\n", "<br>")


def _table(head: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _rate_table(p: Product) -> str:
    o = p.options
    if p.kind == "saving":
        return _table(["기간(개월)", "적립방식", "이자방식", "기본금리(%)", "최고금리(우대 포함, %)"],
                      [[x["term_months"], x["rsrv_type_nm"], x["rate_type_nm"], x["rate"], x["rate_max"]] for x in o])
    if p.kind == "deposit":
        return _table(["기간(개월)", "이자방식", "기본금리(%)", "최고금리(우대 포함, %)"],
                      [[x["term_months"], x["rate_type_nm"], x["rate"], x["rate_max"]] for x in o])
    if p.kind == "mortgage":
        return _table(["담보", "상환방식", "금리유형", "최소(%)", "최대(%)", "평균(%, 참고)"],
                      [[x["mrtg_type_nm"], x["rpay_type_nm"], x["lend_rate_type_nm"], x["min"], x["max"], x["avg"]] for x in o])
    if p.kind == "rent":
        return _table(["상환방식", "금리유형", "최소(%)", "최대(%)", "평균(%, 참고)"],
                      [[x["rpay_type_nm"], x["lend_rate_type_nm"], x["min"], x["max"], x["avg"]] for x in o])
    cols = sorted(o, key=lambda x: x["type"])
    rows = [[lab] + [c["grades"][code] for c in cols] for code, lab in GRADES]
    rows.append(["평균(참고)"] + [c["avg"] for c in cols])
    return _table(["신용점수 구간"] + [c["type_nm"] + "(%)" for c in cols], rows)


def to_markdown(p: Product) -> str:
    b = p.base
    # 오늘 날짜로 판매종료를 판정하지 않는다(시계 의존). 공시 종료일만 사실대로 적는다.
    sale = "판매중" if not b["dcls_end_day"] else f"공시 종료일 {b['dcls_end_day']}"
    fm = {"id": p.id, "kind": p.kind, "kind_ko": KIND_KO[p.kind], "group": p.group, "bank": p.bank,
          "product": p.name, "code": b["fin_prdt_cd"], "dcls_month": b["dcls_month"], "sale": sale,
          "data_flags": len(p.flags)}
    out = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()] + ["---", "",
           f"# {p.bank} {p.name} ({KIND_KO[p.kind]})", ""]
    info = [["금융회사", p.bank], ["금융권", GROUP_KO[p.group]], ["상품명", p.name], ["공시월", b["dcls_month"]],
            ["판매 상태", sale],
            ["가입 방법", b["join_way"]]]
    if p.kind in ("saving", "deposit"):
        info += [["가입 제한", JOIN_DENY.get(b["join_deny"], b["join_deny"])], ["최고 한도(원)", b["max_limit"]]]
    if p.kind == "credit":
        info += [["대출 종류", b["crdt_prdt_type_nm"]], ["CB사", b["cb_name"]]]
    out += ["<!-- retrieval: keyword -->", "## 기본 정보", "", _table(["항목", "값"], info), "",
            "<!-- retrieval: keyword -->", "## 금리", ""]
    out += [_rate_table(p) if p.options else "금리 옵션이 공시되지 않았습니다.", ""]
    if p.flags:
        out += ["<!-- retrieval: keyword -->", "## 데이터 주의", ""] + [f"- {f}" for f in p.flags] + [""]
    texts = [(lab, b[f]) for f, lab in EMBED_FIELDS[p.kind] if b.get(f)]
    if texts:
        out += ["<!-- retrieval: embedding -->", "## 상품 설명", ""]
        for lab, t in texts:
            out += [f"### {lab}", "", t, ""]
    return "\n".join(out)


SECTION_RE = re.compile(r"<!-- retrieval: (keyword|embedding) -->\n")


def split_sections(md: str) -> list[tuple[str, str]]:
    """[(검색방식, 섹션본문)] — 마커 앞의 front matter·제목은 제외한다."""
    parts = SECTION_RE.split(md)
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]
