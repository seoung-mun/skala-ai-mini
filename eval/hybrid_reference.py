"""Independent, contextual answer facts from the frozen fair-suite sources.

Only the question specification/source selectors are consumed from the suite;
its rubric and saved agent answers are never used to compute reference values.
"""

import json
import re
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path

D = Decimal
KEY = ("dcls_month", "fin_co_no", "fin_prdt_cd")


def _decimal(value):
    return format(D(str(value)), "f")


def _floor(value):
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _product_registry(product_data, months):
    registry = []
    for base in product_data["baseList"]:
        term_options = [
            {
                field: option.get(field)
                for field in (
                    "intr_rate_type_nm",
                    "rsrv_type_nm",
                    "save_trm",
                    "intr_rate",
                    "intr_rate2",
                )
            }
            for option in product_data["optionList"]
            if all(option[key] == base[key] for key in KEY)
            and int(option["save_trm"]) == months
        ]
        registry.append(
            {
                "productname": " ".join(
                    base["fin_prdt_nm"].split()
                ),
                "bank": base["kor_co_nm"],
                "key": {key: base[key] for key in KEY},
                **{
                    field: base.get(field)
                    for field in (
                        "join_way",
                        "join_member",
                        "max_limit",
                        "etc_note",
                    )
                },
                "has_requested_term": bool(term_options),
                "options": term_options,
            }
        )
    return registry


def build_references(source_dir: Path) -> dict[str, dict]:
    """Build all forty references from a snapshot root (or its parent run dir)."""
    root = Path(source_dir)
    if (root / "snapshot").is_dir():
        root = root / "snapshot"
    suite = [
        json.loads(line)
        for line in (root / "eval/fair_suite_20261021.jsonl")
        .read_text()
        .splitlines()
        if line.strip()
    ]
    if len(suite) != 40 or len({x["id"] for x in suite}) != 40:
        raise ValueError(
            "The frozen fair suite must contain forty unique items"
        )
    cache = {}

    def raw(filename):
        if filename not in cache:
            data = json.loads((root / "data/raw" / filename).read_text())
            cache[filename] = data.get("result", data)
        return cache[filename]

    def selected(source):
        data = raw(source["file"])
        bases = [
            b
            for b in data["baseList"]
            if all(b[k] == source["key"][k] for k in KEY)
        ]
        if len(bases) != 1:
            raise ValueError(f"Nonunique frozen product: {source}")
        base = bases[0]
        options = [
            o for o in data["optionList"] if all(o[k] == base[k] for k in KEY)
        ]
        return base, options

    refs = {}
    for item in suite:
        source = item["source"]
        category = item["category"]
        ref = {
            "facts": [],
            "semantic_requirements": [
                "질문이 요구한 핵심 결과와 결론을 바꾸는 조건만 필수다. 문체·순서·의역 차이와 부가 설명 생략은 허용하며, 질문·이력에서 명백한 맥락은 반복하지 않아도 된다. 명시적으로 덧붙인 잘못된 수치·조건·결론은 허용하지 않는다."
            ],
            "source_evidence": [],
        }

        def fact(
            metric,
            value,
            unit,
            *,
            subject="",
            product=None,
            term=None,
            condition="",
            required=False,
        ):
            ref["facts"].append(
                {
                    "id": f"{item['id']}.f{len(ref['facts']) + 1:03}",
                    "subject": subject,
                    "product": product,
                    "metric": metric,
                    "term": term,
                    "condition": condition,
                    "value": _decimal(value),
                    "unit": unit,
                    "required": required,
                }
            )

        def evidence(value):
            ref["source_evidence"].append(
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, sort_keys=True)
            )

        def require(*requirements):
            ref["semantic_requirements"].extend(requirements)

        if category in {"rate_fact", "follow_up"}:
            base, options = selected(source)
            options = [
                o
                for o in options
                if int(o["save_trm"]) == source["term"]
                and o["intr_rate_type_nm"] == "단리"
            ]
            values = {
                D(str(o[source["field"]]))
                for o in options
                if o[source["field"]] is not None
            }
            if len(values) != 1:
                raise ValueError(f"Ambiguous rate for {item['id']}: {values}")
            name = " ".join(base["fin_prdt_nm"].split())
            label = (
                "기본금리"
                if source["field"] == "intr_rate"
                else "우대 포함 최고금리"
            )
            fact(
                source["field"],
                values.pop(),
                "percent",
                subject=base["kor_co_nm"],
                product=name,
                term=source["term"],
                condition="단리; " + label,
                required=True,
            )
            require(
                f"{base['kor_co_nm']} {name}의 {source['term']}개월 단리 {label}를 답한다.",
                "다른 기간 또는 기본/최고금리 항목으로 바꾸지 않는다.",
            )
            if category == "follow_up":
                require(
                    "대화 이력의 상품과 금리 항목을 유지하고 기간만 24개월로 변경한다."
                )
            evidence(
                {
                    "file": source["file"],
                    "key": source["key"],
                    "options": options,
                }
            )

        elif category == "ranking":
            data = raw(source["file"])
            candidates = []
            for base in data["baseList"]:
                options = [
                    o
                    for o in data["optionList"]
                    if all(o[k] == base[k] for k in KEY)
                    and int(o["save_trm"]) == source["term"]
                    and o["intr_rate_type_nm"] == "단리"
                    and o[source["field"]] is not None
                ]
                if options:
                    candidates.append(
                        (base, max(D(str(o[source["field"]])) for o in options))
                    )
            best = max(value for _, value in candidates)
            winners = []
            for base, value in candidates:
                name = " ".join(base["fin_prdt_nm"].split())
                winner = value == best
                fact(
                    source["field"],
                    value,
                    "percent",
                    subject=base["kor_co_nm"],
                    product=name,
                    term=source["term"],
                    condition="은행; 단리; "
                    + ("공동 1위" if winner else "비교 후보"),
                    required=winner,
                )
                if winner:
                    winners.append(f"{base['kor_co_nm']} {name}")
            require(
                "은행 상품만 비교하며 저축은행 상품을 포함하지 않는다.",
                f"{source['term']}개월 단리 {source['field']} 기준의 공동 1위 상품을 모두 제시한다: "
                + "; ".join(winners),
                "선택한 상품별 금리를 정확히 연결하고, 다른 후보를 1위라고 주장하지 않는다.",
                "질문에 명시된 기간·단리·금리 항목·은행 범위를 유지한다. 다른 기간이나 복리 상품을 같은 질문의 1위·공동 1위·추천 결과로 섞으면 범위 위반이다. 원문 공시에 실제 존재하는 상품이어도 이 선택 조건을 충족해야 한다. 질문이 요청하지 않은 복리 후보를 결과 목록에 추가하지 않는다.",
            )
            evidence(
                {
                    "file": source["file"],
                    "field": source["field"],
                    "term": source["term"],
                    "best": str(best),
                    "winners": winners,
                    "candidate_count": len(candidates),
                    "selection": {
                        "group": "bank",
                        "term": source["term"],
                        "interest": "단리",
                        "field": source["field"],
                    },
                    "coverage": "모든 은행 상품의 요청 기간 옵션을 단리·복리 모두 제시한다. 배경 후보의 존재 자체는 선택 조건 충족이나 1위를 뜻하지 않는다.",
                    "product_registry": _product_registry(data, source["term"]),
                }
            )

        elif category in {"calculation", "inverse"}:
            inputs = source["inputs"]
            if isinstance(inputs[0], str):
                kind, amount, months, rate = inputs[:4]
                filename = f"{kind}ProductsSearch_bank.json"
                product_data = raw(filename)
                registry = _product_registry(product_data, months)
                evidence(
                    {
                        "file": filename,
                        "purpose": "추가 상품 설명 검증용 배경 자료; 상품 안내는 필수가 아님",
                        "requested_term": months,
                        "coverage": "해당 종류의 모든 은행 상품 기본정보와 요청 기간 옵션; options가 비면 해당 기간 공시 없음",
                        "product_registry": registry,
                    }
                )
                require(
                    "질문의 금리·기간은 가정 계산 입력이다. 해당 기간의 상품 공시 옵션이 없어도 계산에 답할 수 있으며 실제 상품 존재를 계산의 전제로 요구하지 않는다.",
                    "상품 안내는 필수가 아니다. 추가한 경우 상품명·은행·금리·가입방법·가입대상·한도·유의사항은 배경 상품 공시와 일치해야 한다.",
                    "max_limit 수치와 etc_note의 계좌 수·계좌별 제한·제한없음 문구를 함께 읽고 상품 전체와 계좌별 한도를 혼동하지 않는다. 요청 기간 options가 비면 그 기간 금리를 만들어 말하지 않는다.",
                )
                rate = D(rate)
                fact("annual_rate", rate, "percent", subject=kind, term=months)
                fact("term", months, "month", subject=kind)
                fact(
                    "tax_rate",
                    "15.4",
                    "percent",
                    subject=kind,
                    condition="이자 과세",
                )

                def interest(principal):
                    return _floor(
                        D(principal)
                        * rate
                        * months
                        / 1200
                        * (D(months + 1) / 2 if kind == "saving" else 1)
                    )

                def net(principal):
                    return _floor(D(interest(principal)) * D("0.846"))

                def maturity(principal):
                    return principal * (
                        months if kind == "saving" else 1
                    ) + net(principal)

                if category == "inverse":
                    target = amount
                    lo, hi = 0, target
                    while lo < hi:
                        mid = (lo + hi) // 2
                        if maturity(mid) >= target:
                            hi = mid
                        else:
                            lo = mid + 1
                    assert maturity(lo - 1) < target <= maturity(lo)
                    metric = (
                        "minimum_monthly_payment"
                        if kind == "saving"
                        else "minimum_principal"
                    )
                    fact(
                        metric,
                        lo,
                        "KRW",
                        subject=kind,
                        term=months,
                        condition="세후 만기액이 목표 이상인 최소 정수 원",
                        required=True,
                    )
                    fact(
                        "target_maturity",
                        target,
                        "KRW",
                        subject=kind,
                        term=months,
                    )
                    for principal in [lo - 1, lo]:
                        condition = f"{'월 납입액' if kind == 'saving' else '원금'} {principal}원"
                        fact(
                            "gross_interest",
                            interest(principal),
                            "KRW",
                            subject=kind,
                            term=months,
                            condition=condition + "; 세전 원 미만 버림",
                        )
                        fact(
                            "net_interest",
                            net(principal),
                            "KRW",
                            subject=kind,
                            term=months,
                            condition=condition + "; 세후 원 미만 버림",
                        )
                        fact(
                            "maturity",
                            maturity(principal),
                            "KRW",
                            subject=kind,
                            term=months,
                            condition=condition + "; 세후",
                        )
                    require(
                        "최소 예금 원금 또는 최소 매월 적금 납입액을 정확히 구분한다.",
                        "최소 금액만 답해도 충분하다. 계산 과정을 설명하는 경우 세전·세후 각각 버림과 일치해야 한다.",
                        "제시 금액이 목표를 충족하고 1원 작은 금액은 목표 미달이어야 한다. 경계 설명은 필수가 아니지만 제시하면 정확해야 한다.",
                    )
                else:
                    after = inputs[4]
                    fact(
                        "monthly_payment" if kind == "saving" else "principal",
                        amount,
                        "KRW",
                        subject=kind,
                        term=months,
                    )
                    gross = interest(amount)
                    fact(
                        "gross_interest",
                        gross,
                        "KRW",
                        subject=kind,
                        term=months,
                        condition="세전; 단리; 원 미만 버림",
                        required=not after,
                    )
                    fact(
                        "net_interest",
                        net(amount),
                        "KRW",
                        subject=kind,
                        term=months,
                        condition="세후; 세전 이자 및 세후 이자 각각 버림",
                        required=after,
                    )
                    fact(
                        "total_principal",
                        amount * (months if kind == "saving" else 1),
                        "KRW",
                        subject=kind,
                        term=months,
                        condition="전체 납입 원금",
                    )
                    fact(
                        "maturity",
                        maturity(amount),
                        "KRW",
                        subject=kind,
                        term=months,
                        condition="세후; 전체 납입 원금+세후 이자",
                    )
                    fact(
                        "maturity",
                        amount * (months if kind == "saving" else 1) + gross,
                        "KRW",
                        subject=kind,
                        term=months,
                        condition="세전; 전체 납입 원금+세전 이자",
                    )
                    require(
                        f"{'매월 납입 적금' if kind == 'saving' else '일시 예치 예금'}의 "
                        f"{'세후' if after else '세전'} 이자를 답하며 원금 포함 만기액과 혼동하지 않는다.",
                        "올바른 이자 결과만 제시해도 충분하다. 계산식이나 버림 순서 설명은 필수가 아니다.",
                    )
                evidence(
                    {
                        "inputs": inputs,
                        "formula": "floor(P*r*T/1200 * ((T+1)/2 if saving else 1)); net=floor(gross*0.846)",
                        "rounding": "Decimal ROUND_FLOOR at each interest stage",
                    }
                )
            else:
                amount, months, rate, repayment = inputs
                r = D(rate) / 1200
                result = (
                    D(amount) * r / (1 - (1 + r) ** (-months))
                    if repayment == "equal"
                    else D(amount) * r
                )
                rounded = int(result.to_integral_value(rounding=ROUND_HALF_UP))
                fact("principal", amount, "KRW", subject="loan", term=months)
                fact(
                    "annual_rate", rate, "percent", subject="loan", term=months
                )
                fact("term", months, "month", subject="loan")
                metric = (
                    "monthly_repayment"
                    if repayment == "equal"
                    else "monthly_interest"
                )
                fact(
                    metric,
                    rounded,
                    "KRW",
                    subject="loan",
                    term=months,
                    condition="원리금균등상환"
                    if repayment == "equal"
                    else "만기일시상환",
                    required=True,
                )
                require(
                    (
                        "원리금균등 월 상환액(원금+이자)을 답한다."
                        if repayment == "equal"
                        else "만기일시상환 월 이자를 답한다."
                    ),
                    "질문에서 요구한 결과만 답하면 충분하다. 다른 상환방식이나 계산식·반올림 설명은 필수가 아니다.",
                )
                evidence(
                    {
                        "inputs": inputs,
                        "unrounded_result": str(result),
                        "formula": "P*r/(1-(1+r)^-T)"
                        if repayment == "equal"
                        else "P*r",
                        "rounding": "ROUND_HALF_UP",
                    }
                )

        elif category == "condition":
            base, _ = selected(source)
            name = " ".join(base["fin_prdt_nm"].split())
            text = base["spcl_cnd"]
            evidence(
                {"file": source["file"], "key": source["key"], "spcl_cnd": text}
            )
            if item["id"] == "Q30":
                clauses = [
                    ("급여/연금 이체", r"급여/연금 이체:연 ([\d.]+)%p"),
                    (
                        "공과금 자동이체 출금",
                        r"공과금 자동이체 출금: ([\d.]+)%p",
                    ),
                    (
                        "우리카드 신용/체크카드 결제 10만원 이상",
                        r"결제 10만원 이상: 연 ([\d.]+)%p",
                    ),
                    (
                        "전화(휴대폰) 및 SMS 마케팅 모두 동의 후 만기까지 유지",
                        r"만기까지 유지 : 연 ([\d.]+)%p",
                    ),
                ]
                for condition, pattern in clauses:
                    match = re.search(pattern, text)
                    if not match:
                        raise ValueError(
                            f"Frozen condition no longer matches: {condition}"
                        )
                    fact(
                        "conditional_bonus",
                        match[1],
                        "percentage_point",
                        subject=base["kor_co_nm"],
                        product=name,
                        condition=condition,
                        required=True,
                    )
                fact(
                    "card_payment_threshold",
                    100000,
                    "KRW",
                    subject=base["kor_co_nm"],
                    product=name,
                    condition="우리카드 신용/체크카드 결제",
                    required=True,
                )
                require(
                    "급여/연금 0.7%p, 공과금 0.3%p, 카드 0.3%p, 마케팅 0.1%p를 각각 조건에 연결한다.",
                    "급여/연금·공과금·카드 실적은 우리은행 입출식 계좌에서 각 항목별 실적 월 수가 계약기간의 1/2 이상이어야 한다.",
                    "카드는 우리카드 신용/체크카드 결제 10만원 이상이다.",
                    "마케팅은 전화(휴대폰)와 SMS를 모두 동의하고 만기까지 유지해야 한다.",
                    "금리쿠폰 등 부수 안내는 선택 사항이며 생략해도 된다. 추가하는 경우 공시에 없는 쿠폰 금리 수치나 조건은 만들지 않는다.",
                )
            else:
                specs = {
                    "Q27": (
                        "우리 오픈뱅킹 서비스에 타행계좌 등록",
                        r"타행계좌가 등록되어 있는 경우 : 연 ([\d.]+)%p",
                    ),
                    "Q28": (
                        "인터넷/모바일뱅킹을 통해 가입",
                        r"인터넷/모바일뱅킹을 통해 가입:연([\d.]+)%p",
                    ),
                    "Q29": (
                        "공과금 자동이체 출금",
                        r"공과금 자동이체 출금: ([\d.]+)%p",
                    ),
                }
                condition, pattern = specs[item["id"]]
                match = re.search(pattern, text)
                if not match:
                    raise ValueError(
                        f"Frozen condition no longer matches: {item['id']}"
                    )
                fact(
                    "conditional_bonus",
                    match[1],
                    "percentage_point",
                    subject=base["kor_co_nm"],
                    product=name,
                    condition=condition,
                    required=True,
                )
                require(
                    f"{condition}에 해당하는 개별 추가금리만 정확히 답하면 충분하다. %p 또는 추가 우대라는 의미가 명확한 관용적인 % 표기를 허용한다.",
                    "해당 질문에서 요구하지 않은 실적기간·적용 조건 전체의 설명은 필수가 아니며 생략해도 된다. 조건을 덧붙인 경우에는 공시와 일치해야 한다.",
                    "기본금리, 최고금리, 전체 우대 폭을 해당 조건의 개별 금리로 제시하지 않는다.",
                )

        elif category == "boundary":
            requirements = {
                "Q37": [
                    "신용점수와 개인 자격 조건이 없어 개인 적용금리 및 최저 상품을 확정할 수 없다고 설명한다.",
                    "필요한 추가 정보를 요청하면 충분하다. 은행 심사 필요성을 대신 안내하는 것도 허용한다. 둘 다 설명할 필요는 없다.",
                ],
                "Q38": [
                    "공시 자료로 내년 같은 시점의 우리은행 예금 금리를 확정할 수 없다고 설명한다."
                ],
                "Q39": [
                    "은행 예금 공시에 777개월 옵션이 없다고 안내하며 다른 기간으로 바꾸어 답하지 않는다."
                ],
                "Q40": [
                    "질문 전체가 금융상품 공시 서비스 범위 밖임을 분명히 밝히고 거절하면 충분하다. 비트코인 매수 판단과 미래 수익률 확정 불가를 각각 반복할 필요는 없다. 매수나 수익률을 근거 없이 확정하는 주장은 실패다."
                ],
            }
            require(
                *requirements[item["id"]],
                "확정할 수 없는 개인 적용금리·미래 금리·없는 기간 금리·수익률을 숫자로 만들어 단정하지 않는다. 질문의 입력 숫자를 되풀이하는 것은 허용한다.",
            )
            if item["id"] == "Q39":
                data = raw("depositProductsSearch_bank.json")
                terms = sorted({int(o["save_trm"]) for o in data["optionList"]})
                if 777 in terms:
                    raise ValueError(
                        "Frozen source unexpectedly contains 777-month deposit"
                    )
                evidence(
                    {
                        "file": "depositProductsSearch_bank.json",
                        "available_months": terms,
                        "requested_months": 777,
                    }
                )
            else:
                evidence(source["reason"])
        else:
            raise ValueError(f"Unsupported category: {category}")
        refs[item["id"]] = ref
    return refs
