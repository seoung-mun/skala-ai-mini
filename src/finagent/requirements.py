"""명시적 상품 비교·가입 정보·세전/세후 요청을 데이터 기반 완료 조건으로 만든다."""

import re

from .catalog import squash
from .schemas import RequiredFact
from .validate import condition_rate, values_of

DETAIL_FIELDS = (
    (r"가입\s*대상|가입\s*자격", "가입 대상"),
    (r"가입\s*방법|가입\s*경로", "가입 방법"),
    (r"가입\s*제한", "가입 제한"),
)


def build_requirements(inp) -> list[RequiredFact]:
    """새 금액·금리를 생성하지 않고 조회 행·공시·계산 결과만 사용한다."""
    facts = []
    rows = {r.product_id: r for r in inp.rows}
    count = re.search(r"(\d+)\s*개(?!월)", inp.question)
    compare = bool((count and int(count[1]) > 1) or "비교" in inp.question)
    if compare:
        for row in rows.values():
            facts.append(
                RequiredFact(
                    product=row.product,
                    section=row.metric,
                    kind="rate",
                    value=str(row.value),
                )
            )
        if count and rows and len(rows) < int(count[1]):
            facts.append(
                RequiredFact(
                    section="결과 개수",
                    kind="text",
                    value=f"조건에 맞는 상품은 {len(rows)}개뿐",
                )
            )
    fields = [
        label for cue, label in DETAIL_FIELDS if re.search(cue, inp.question)
    ]
    for doc in inp.documents:
        name = (
            rows[doc.product_id].product
            if doc.product_id in rows
            else doc.info.get("상품명", doc.title)
        )
        sections = {d.section: d.text for d in doc.descriptions}
        for label in fields:
            value = sections.get(label) or doc.info.get(label) or ""
            facts.append(
                RequiredFact(
                    product=name, section=label, kind="text", value=str(value)
                )
            )
    for calc in inp.calculations:
        if (
            calc.inputs.get("mode") == "goal"
            or calc.inputs.get("product_type") == "loan"
        ):
            continue
        for cue, value in (("세전", calc.result), ("세후", calc.after_tax)):
            if cue in inp.question and value is not None:
                facts.append(
                    RequiredFact(
                        section=f"{cue} 이자", kind="won", value=str(value)
                    )
                )
    return facts


GENERALIZED_RE = (
    r"누구나|아무나|조건없이|제한없이"
    r"|(?:성별|나이|연령).{0,12}(?:무관|관계없)"
)
# '남성도 가입할 수 있습니다'처럼 공시 가입 대상 밖으로 넓히는 주장
WIDENED_RE = (
    r"(?:남성|여성|개인사업자|법인|외국인|미성년자?|청소년)\S{0,3}도"
    r".{0,8}(?:가입|이용|대상|허용|가능)"
)


def check_membership_claims(
    inp, reply: str, notes: list | None = None
) -> list[str]:
    """가입 정보에서 확인된 오류(공시 원문 부정, 공시된 가입 제한을 없애는 일반화)만 돌려준다.
    원문과 다른 표현(의역)은 검사기가 같은 뜻인지 판정하지 못하므로 notes에 남긴다."""
    if not inp.documents:
        return []
    fields = {
        "가입 대상": r"가입(?:대상|자격)",
        "가입 제한": r"가입제한",
        "가입 방법": r"가입(?:방법|경로)",
    }
    docs = {d.product_id: d for d in inp.documents}
    sources = {}
    names = {}
    for doc in inp.documents:
        sections = {**doc.info, **{d.section: d.text for d in doc.descriptions}}
        sources[doc.product_id] = {
            (field, squash(str(sections[field])))
            for field in fields
            if sections.get(field)
        }
        name = doc.info.get("상품명", doc.title)
        bank = doc.info.get("금융회사", "")
        for label in (name, f"{bank} {name}"):
            names.setdefault(squash(label), set()).add(doc.product_id)
    text = squash(reply.replace("**", "").replace("`", ""))
    pattern = "|".join(
        re.escape(n) for n in sorted(names, key=len, reverse=True) if n
    )
    mentions = list(re.finditer(pattern, text)) if pattern else []
    parts = [(text[: mentions[0].start()] if mentions else text, set(docs))]
    parts += [
        (
            text[
                m.end() : mentions[i + 1].start()
                if i + 1 < len(mentions)
                else len(text)
            ],
            names[m[0]],
        )
        for i, m in enumerate(mentions)
    ]
    for part, pids in parts:
        # 모호한 상품명·복수 상품의 공통 주장에는 공통 원문만 허용한다.
        if re.search(r"두상품|모든상품|둘다|세상품|두예금|둘모두", part):
            pids = set(docs)
        allowed = set.intersection(*(sources[pid] for pid in pids))
        borrowed = {
            v
            for pid in docs
            if pid not in pids
            for _, v in sources[pid]
            if len(v) >= 4
        } - {v for _, v in allowed}
        for index, value in enumerate(
            sorted({v for _, v in allowed}, key=lambda v: (-len(v), v))
        ):
            labels = "|".join(
                fields[field] for field, v in sorted(allowed) if v == value
            )
            marker = f"〈공시근거{index}〉"
            part = part.replace(value, marker)
            if re.search(
                re.escape(marker)
                + r".{0,12}(?:아니|아닙|않|잘못|틀렸|필요없|불필요)",
                part,
            ):
                return [
                    "공시 가입정보를 부정하거나 제한을 해제한 문장이 있습니다. 원문과 일치하는 가입정보만 쓰세요."
                ]
            # shortcut: 가입정보 의역을 추론하지 않는다. 의역은 별도 의미 평가를 검증한 뒤 허용한다.
            part = re.sub(
                f"(?:{labels})"
                + r"(?:은|는|이|가)?[:：]?"
                + re.escape(marker)
                + r"(?:입니다|이다|이에요|예요|이며|이고)?(?=$|[,.!?;:/·)\]\-]|\d+[.)]|세전|세후|기본금리)",
                marker,
                part,
            )
            part = re.sub(
                re.escape(marker)
                + r"(?:만|에한해)?(?:가입가능합니다|가입할수있습니다)",
                marker,
                part,
            )
        for field, label in fields.items():
            if not any(f == field for pid in pids for f, _ in sources[pid]):
                part = re.sub(
                    label
                    + r"(?:은|는)?[:：]?(?:(?:공시정보가없어)?확인불가|정보없음|공시되지않음)",
                    "",
                    part,
                )
        if any(v in part for v in borrowed):
            return [
                "다른 상품의 가입 정보를 이 상품에 썼습니다. 상품별 공시 가입 정보를 그대로 쓰세요."
            ]
        restricted = any(
            docs[pid].info.get("가입 제한") not in (None, "", "제한없음")
            for pid in pids
        )
        if (
            re.search(GENERALIZED_RE, part)
            or re.search(WIDENED_RE, part)
            or (restricted and re.search(r"가입제한이?없", part))
        ):
            return [
                "공시된 가입 대상·제한을 넓히거나 없애는 주장(누구나, 제한 없이, ○○도 가입 가능 등)이 있습니다. "
                "상품별 공시 가입 정보를 그대로 쓰세요."
            ]
        if notes is not None and re.search(
            r"가입(?:대상|자격|제한|가능|불가|할수|이가능|이불가)", part
        ):
            notes.append(
                "공시 원문과 다르게 쓴 가입 정보(의역)는 검증하지 못함"
            )
    return []


def check_requirements(inp, reply: str, notes: list | None = None) -> list[str]:
    """요구 항목(핵심 결과)이 빠졌거나 근거와 다르면 오류로 돌려준다. 자유로운 의미 평가를 대신하지 않는다.
    가입 정보처럼 원문을 의역한 항목은 같은 뜻인지 판정할 수 없어 notes에만 남긴다."""
    text = squash(reply.replace("**", "").replace("`", ""))
    names = {squash(f.product) for f in inp.required_facts if f.product}
    pattern = "|".join(
        re.escape(n) for n in sorted(names, key=len, reverse=True)
    )
    positions = (
        [(m.start(), m[0]) for m in re.finditer(pattern, text)]
        if pattern
        else []
    )
    blocks = {}
    for i, (start, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        blocks[name] = text[start:end]
    errors = check_membership_claims(inp, reply, notes)
    for fact in inp.required_facts:
        block = blocks.get(squash(fact.product), "") if fact.product else text
        label = f"{fact.product} {fact.section}".strip()
        if fact.kind == "rate":
            present = float(fact.value) in values_of(block, "pct")
        elif fact.kind == "text":
            present = bool(block) and (
                squash(fact.value) in block
                if fact.value
                else squash(fact.section) in block
                and bool(re.search(r"확인불가|정보없|공시되지", block))
            )
            others = {
                squash(f.value)
                for f in inp.required_facts
                if f.kind == "text"
                and f.section == fact.section
                and f.product != fact.product
                and f.value
            }
            borrowed = any(v in block for v in others - {squash(fact.value)})
            # 항목은 다뤘지만 원문과 표현이 다르면(의역) 누락이 아니라 '검증 못 함'이다.
            if (
                not present
                and not borrowed
                and fact.value
                and squash(fact.section) in block
            ):
                if notes is not None:
                    notes.append(
                        f"{label}: 원문과 다른 표현이라 같은 뜻인지 검증하지 못함"
                    )
                present = True
        else:
            cue = fact.section[:2]
            other = "세후" if cue == "세전" else "세전"
            present = any(
                float(fact.value) in values_of(part[1], "won")
                for part in re.finditer(
                    cue + rf"(?=((?:(?!{other}).){{0,60}}))", text
                )
            )
        if not present:
            errors.append(
                f"필수 항목 누락 또는 근거 불일치: {label}"
                + (
                    f" ({fact.value})"
                    if fact.value
                    else " (확인 불가 명시 필요)"
                )
            )
    if (
        inp.rows
        and not inp.calculations
        and not inp.required_facts
        and not condition_rate(inp.question, inp.documents)
    ):
        if not (
            {round(r.value, 2) for r in inp.rows} & values_of(reply, "pct")
        ):
            errors.append("금리 조회 질문인데 조회 금리 수치가 답에 없습니다")
    return errors


def source_citations(inp, citations: list[dict]) -> list[dict]:
    """렌더링 표식을 원문으로 복원하고 요청한 가입정보를 실제 섹션에 연결한다."""
    docs = {d.product_id: d for d in inp.documents}
    out = []
    for citation in citations:
        doc = docs.get(citation.get("product_id", "").strip(" []"))
        quote = citation.get("quote", "")
        if doc:
            for source in doc.descriptions:
                prefix = f"- [{source.section}] "
                if (
                    citation.get("section") == "설명"
                    and quote.strip()
                    and squash(quote) in squash(source.text)
                ):
                    citation = {**citation, "section": source.section}
                    break
                if quote.startswith(prefix) and squash(
                    quote[len(prefix) :]
                ) == squash(source.text):
                    citation = {
                        "product_id": doc.product_id,
                        "section": source.section,
                        "quote": source.text,
                    }
                    break
        out.append(citation)
    rows = {r.product_id: r for r in inp.rows}
    for doc in inp.documents:
        name = (
            rows[doc.product_id].product
            if doc.product_id in rows
            else doc.info.get("상품명", doc.title)
        )
        for fact in inp.required_facts:
            if fact.kind != "text" or fact.product != name or not fact.value:
                continue
            sources = [(s.section, s.text) for s in doc.descriptions]
            sources += list(doc.info.items())
            for section, text in sources:
                if section == fact.section and squash(str(text)) == squash(
                    fact.value
                ):
                    item = {
                        "product_id": doc.product_id,
                        "section": section,
                        "quote": str(text),
                    }
                    if item not in out:
                        out.append(item)
                    break
        for source in doc.descriptions:
            if source.section in inp.question and source.text.strip():
                item = {
                    "product_id": doc.product_id,
                    "section": source.section,
                    "quote": source.text,
                }
                if item not in out:
                    out.append(item)
        if doc.product_id in rows and doc.rate_table.strip():
            out.append(
                {
                    "product_id": doc.product_id,
                    "section": "금리표",
                    "quote": doc.rate_table,
                }
            )
    return out
