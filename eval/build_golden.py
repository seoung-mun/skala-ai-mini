"""골든셋 생성: 정답을 파싱된 실데이터에서 코드로 계산한다. difficulty는 라우팅 정답(easy=즉답 경로, hard=숙고 경로)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from finagent.catalog import Catalog, core_name
from finagent.parser import parse_all

ALL = parse_all(ROOT / "data/raw")
P = [p for p in ALL if p.group == "bank"]
def bank_ko(b: str) -> str:
    return b.replace("주식회사", "").strip()  # 답변은 '농협은행'처럼 쓰지 '농협은행주식회사'라고 쓰지 않는다


BANKS = sorted({bank_ko(p.bank) for p in ALL})


CAT = Catalog(ALL)


def names(kind: str) -> list[str]:
    """채점 후보: 이름만으로 상품을 특정할 수 있는 이름(일반명 '정기예금' 등은 제외)."""
    return sorted({p.name for p in ALL if p.kind == kind and core_name(p.name) not in CAT.generic})


def one(kind: str, name: str, bank: str | None = None):
    m = [p for p in P if p.kind == kind and p.name == name and (bank is None or bank in p.bank)]
    assert len(m) == 1, f"{kind}/{bank}/{name}: {len(m)}건"
    return m[0]


def text_has(p, field: str, *phrases: str):
    for ph in phrases:
        assert ph in p.base[field], f"{p.name}.{field}에 '{ph}' 없음"


def rates(p, term: int, key: str) -> set[float]:
    return {o[key] for o in p.options if o["term_months"] == term and o[key] is not None}


def argbest(cands: list[tuple[float, object]], low: bool = False) -> list:
    best = (min if low else max)(v for v, _ in cands)
    return [p for v, p in cands if v == best]


items = []


def add(id_, difficulty, category, query, gold, check, retrieval="any"):
    items.append({"id": id_, "difficulty": difficulty, "category": category, "query": query,
                  "gold_ids": [g.id for g in gold], "retrieval": retrieval if gold else None, "check": check})


# ---- easy: 단일 상품 사실 조회 ----
woori = one("saving", "우리SUPER주거래적금")
r12 = rates(woori, 12, "rate_max"); assert len(r12) == 1
add("E1", "easy", "fact", "우리은행 우리SUPER주거래적금 12개월 최고우대금리 얼마야?", [woori],
    {"type": "nums_any", "values": sorted(r12)})
k26 = one("saving", "카카오뱅크 26주적금"); text_has(k26, "spcl_cnd", "최고 연 3.00%p")
add("E2", "easy", "fact", "카카오뱅크 26주적금 우대금리 최고 몇 %p야?", [k26], {"type": "nums_any", "values": [3.0]})
toss31 = one("saving", "토스뱅크 키워봐요 31일적금"); text_has(toss31, "spcl_cnd", "31일 저금 성공 시 : 연 9.0%")
add("E3", "easy", "fact", "토스뱅크 키워봐요 31일적금 31일 다 성공하면 우대금리 몇 %야?", [toss31],
    {"type": "nums_any", "values": [9.0]})
nh = one("saving", "NH고향사랑기부적금"); text_has(nh, "spcl_cnd", "MZ고객 우대 : 0.3%p")
add("E4", "easy", "fact", "NH고향사랑기부적금 MZ고객 우대금리 얼마야?", [nh], {"type": "nums_any", "values": [0.3]})
sol = one("saving", "신한 알.쏠 적금"); text_has(sol, "spcl_cnd", "소득이체 : 연 0.6%")
add("E5", "easy", "fact", "신한 알.쏠 적금 소득이체 하면 우대금리 얼마 줘?", [sol], {"type": "nums_any", "values": [0.6]})
cr = [p for p in P if p.kind == "credit" and "우리" in p.bank and p.base["crdt_prdt_type"] == "1"]
assert len(cr) == 1, len(cr)
cr = cr[0]
a900 = next(o for o in cr.options if o["type"] == "A")["grades"]["crdt_grad_1"]; assert a900 is not None
add("E6", "easy", "fact", f"우리은행 {cr.name} 일반신용대출, 신용점수 950점이면 대출금리 몇 %야?", [cr],
    {"type": "nums_any", "values": [a900]})

# ---- easy: 표현이 다른 질문(키워드가 원문과 안 겹침) ----
add("S1", "easy", "semantic", "월급 받는 통장을 우리은행으로 옮기면 우리SUPER주거래적금 금리 얼마나 더 붙어?", [woori],
    {"type": "nums_any", "values": [0.7]})
text_has(woori, "spcl_cnd", "급여/연금 이체:연 0.7%p")
kid = one("saving", "카카오뱅크 우리아이적금"); text_has(kid, "spcl_cnd", "연 4.00%p")
add("S2", "easy", "semantic", "카카오뱅크에서 애 앞으로 드는 적금, 자동이체로 넣으면 금리 몇 %p 더 줘?", [kid],
    {"type": "nums_any", "values": [4.0]})
kq = one("saving", "궁금한 적금"); text_has(kq, "spcl_cnd", "랜덤", "최고 연 6.0%")
add("S3", "easy", "semantic", "케이뱅크에 돈 넣을 때마다 이자가 뽑기처럼 붙는 적금 있던데 최대 몇 %까지야?", [kq],
    {"type": "nums_any", "values": [6.0]})
kb = one("saving", "KB맑은하늘적금"); text_has(kb, "spcl_cnd", "1년제 최고 연 0.8%p")
add("S4", "easy", "semantic", "국민은행에 환경 지키면 금리 더 주는 적금 있지? 1년짜리는 최대 몇 %p야?", [kb],
    {"type": "nums_any", "values": [0.8]})

# ---- hard: 비교·집계·계산 ----
sv = [p for p in P if p.kind == "saving"]
c = [(max(rates(p, 12, "rate")), p) for p in sv if rates(p, 12, "rate")]
w = argbest(c)
add("H1", "hard", "compare", "은행 적금 중에 12개월 기본금리가 제일 높은 상품 뭐야?", w,
    {"type": "top_choice", "values": [p.name for p in w], "candidates": names("saving")})
dp = [p for p in P if p.kind == "deposit"]
c = [(max(rates(p, 12, "rate_max")), p) for p in dp if rates(p, 12, "rate_max")]
w = argbest(c)
add("H2", "hard", "compare", "은행 정기예금 12개월짜리 중 최고우대금리 가장 높은 데 어디야?", w,
    {"type": "top_choice", "values": [p.name for p in w], "candidates": names("deposit")})
fixed12 = [o["rate"] for o in woori.options if o["term_months"] == 12 and o["rsrv_type_nm"] == "정액적립식"]
assert len(fixed12) == 1
interest = int(500_000 * 12 * 13 / 2 * fixed12[0] / 1200)
add("H3", "hard", "calc", "우리SUPER주거래적금 정액적립식 12개월에 월 50만원씩 넣으면 기본금리로 단리 세전이자 얼마야?",
    [woori], {"type": "nums_any", "values": [interest]})
won = one("saving", "WON적금")
rw = rates(won, 12, "rate"); rs = rates(woori, 12, "rate"); assert len(rw) == 1 and len(rs) == 1, (rw, rs)
add("H4", "hard", "compare", "우리은행 우리SUPER주거래적금이랑 WON적금 12개월 기본금리 비교해줘", [woori, won],
    {"type": "nums_all", "values": sorted(rs | rw)}, retrieval="all")
c1 = [p for p in P if p.kind == "credit" and p.base["crdt_prdt_type"] == "1"]
c = [(o["grades"]["crdt_grad_5"], p) for p in c1 for o in p.options
     if o["type"] == "A" and o["grades"]["crdt_grad_5"] is not None]
w = argbest(c, low=True)
add("H5", "hard", "compare", "신용점수 750점이면 은행 일반신용대출 중에 대출금리 제일 낮은 곳 어디야?", w,
    {"type": "top_choice", "values": sorted({bank_ko(p.bank) for p in w}), "candidates": BANKS})
r = 4.5 / 1200
pay = round(100_000_000 * r / (1 - (1 + r) ** -360))
add("H6", "hard", "calc", "1억원을 30년 원리금균등상환, 연 4.5%로 빌리면 월 상환액 얼마야?", [],
    {"type": "nums_any", "values": [pay]})
mg = [p for p in P if p.kind == "mortgage"]
c = [(o["min"], p) for p in mg for o in p.options if o["mrtg_type_nm"] == "아파트"
     and o["rpay_type_nm"] == "분할상환방식" and o["lend_rate_type_nm"] == "고정금리" and o["min"] is not None]
w = argbest(c, low=True)
add("H7", "hard", "compare", "아파트 담보로 분할상환 고정금리 주담대 받으려는데 최저금리 제일 낮은 은행 어디야?", w,
    {"type": "top_choice", "values": sorted({bank_ko(p.bank) for p in w}), "candidates": BANKS})
toss = one("saving", "토스뱅크 굴비 적금")
m26, mt = max(o["rate_max"] for o in k26.options), max(o["rate_max"] for o in toss.options)
assert m26 != mt
add("H8", "hard", "compare", "카카오뱅크 26주적금이랑 토스뱅크 굴비 적금 중에 최고우대금리 더 높은 거 뭐야?", [k26, toss],
    {"type": "top_choice", "values": [(k26 if m26 > mt else toss).name], "candidates": names("saving")},
    retrieval="all")

# ======== 추가 20문항 (예금·대출·전세·후속질문 균형) ========
def grade(p, code, typ="A"):
    v = next(o for o in p.options if o["type"] == typ)["grades"][code]
    assert v is not None, (p.name, code)
    return v


busan = one("deposit", "더(The) 특판 정기예금"); text_has(busan, "spcl_cnd", "우대이율 1.25%p")
add("N1", "easy", "fact", "부산은행 더(The) 특판 정기예금 신규고객 우대이율 얼마야?", [busan], {"type": "nums_any", "values": [1.25]})
kkd = one("deposit", "카카오뱅크 정기예금"); r = rates(kkd, 12, "rate"); assert len(r) == 1
add("N2", "easy", "fact", "카카오뱅크 정기예금 12개월 기본금리 얼마야?", [kkd], {"type": "nums_any", "values": sorted(r)})
shm = one("mortgage", "신한주택대출(아파트)"); text_has(shm, "erly_rpay_fee", "고정금리0.59%")
add("N3", "easy", "fact", "신한은행 신한주택대출(아파트) 중도상환해약금률 몇 %야?", [shm], {"type": "nums_any", "values": [0.59]})
kbr = one("rent", "KB주택전세자금대출"); lo = {o["min"] for o in kbr.options}; assert len(lo) == 1
add("N4", "easy", "fact", "국민은행 KB주택전세자금대출 최저금리 얼마야?", [kbr], {"type": "nums_any", "values": sorted(lo)})
kbc = [p for p in P if p.kind == "credit" and p.bank == "국민은행" and p.base["crdt_prdt_type"] == "1"]
assert len(kbc) == 1
add("N5", "easy", "fact", "국민은행 일반신용대출, 신용점수 850점이면 대출금리 몇 %야?", kbc,
    {"type": "nums_any", "values": [grade(kbc[0], "crdt_grad_4")]})
ibk = one("saving", "IBK중기근로자우대적금 (자유적립식)"); text_has(ibk, "spcl_cnd", "연 1.0%p")
add("N6", "easy", "fact", "IBK중기근로자우대적금 급여이체 실적 있으면 우대금리 얼마야?", [ibk], {"type": "nums_any", "values": [1.0]})
tkid = one("saving", "토스뱅크 아이 적금"); text_has(tkid, "spcl_cnd", "연 2.50%")
add("N7", "easy", "semantic", "토스뱅크에 애 앞으로 드는 적금, 자동이체 꼬박꼬박 하면 금리 얼마나 더 줘?", [tkid],
    {"type": "nums_any", "values": [2.5]})
nhg = one("deposit", "NH내가Green초록세상예금"); text_has(nhg, "spcl_cnd", "0.4%p")
add("N8", "easy", "semantic", "농협에 환경 지키겠다고 서약하면 금리 더 주는 예금 있지? 최대 우대 몇 %p야?", [nhg],
    {"type": "nums_any", "values": [0.4]})
shf = one("deposit", "Sh첫만남우대예금"); text_has(shf, "spcl_cnd", "최대우대금리:1.05%")
add("N9", "easy", "semantic", "수협에 처음 거래하면 우대 많이 주는 예금 있던데 최대 우대금리 얼마야?", [shf],
    {"type": "nums_any", "values": [1.05]})
kmo = one("saving", "카카오뱅크 한달적금"); text_has(kmo, "spcl_cnd", "최고 연 5.50%p")
add("N10", "easy", "semantic", "카카오뱅크에 한 달 동안 매일 돈 넣는 적금 우대 최고 몇 %p야?", [kmo],
    {"type": "nums_any", "values": [5.5]})
SB = [p for p in ALL if p.group == "savingsbank"]
c = [(max(rates(p, 12, "rate")), p) for p in SB if p.kind == "deposit" and rates(p, 12, "rate")]
w = argbest(c)
add("N11", "hard", "compare", "저축은행 정기예금 중에 12개월 기본금리 가장 높은 곳 어디야?", w,
    {"type": "top_choice", "values": sorted({bank_ko(p.bank) for p in w}), "candidates": BANKS})
c = [(o["min"], p) for p in P if p.kind == "rent" for o in p.options if o["min"] is not None]
w = argbest(c, low=True)
add("N12", "hard", "compare", "은행 전세대출 중에 최저금리 가장 낮은 곳 어디야?", w,
    {"type": "top_choice", "values": sorted({bank_ko(p.bank) for p in w}), "candidates": BANKS})
c2 = [p for p in P if p.kind == "credit" and p.base["crdt_prdt_type"] == "2"]
c = [(o["grades"]["crdt_grad_4"], p) for p in c2 for o in p.options if o["type"] == "A" and o["grades"]["crdt_grad_4"] is not None]
w = argbest(c, low=True)
add("N13", "hard", "compare", "신용점수 850점이면 은행 마이너스한도대출 중에 금리 제일 낮은 곳 어디야?", w,
    {"type": "top_choice", "values": sorted({bank_ko(p.bank) for p in w}), "candidates": BANKS})
r = rates(kkd, 12, "rate")
add("N14", "hard", "calc", "카카오뱅크 정기예금 12개월에 1천만원 넣으면 기본금리로 세전이자 얼마야?", [kkd],
    {"type": "nums_any", "values": [int(10_000_000 * min(r) * 12 / 1200)]})
add("N15", "hard", "calc", "2억을 10년 만기일시상환으로 연 4%에 빌리면 한 달 이자 얼마야?", [],
    {"type": "nums_any", "values": [round(200_000_000 * 4 / 1200)]})
add("N16", "hard", "calc", "월 30만원씩 24개월, 연 3.5% 단리 적금이면 세전이자 얼마야?", [],
    {"type": "nums_any", "values": [int(300_000 * 24 * 25 / 2 * 3.5 / 1200)]})
mk, mt2 = max(o["rate_max"] for o in kid.options), max(o["rate_max"] for o in tkid.options)
assert mk != mt2
add("N17", "hard", "compare", "카카오뱅크 우리아이적금이랑 토스뱅크 아이 적금 중에 우대 포함 최고금리 더 높은 거 뭐야?", [kid, tkid],
    {"type": "top_choice", "values": [(kid if mk > mt2 else tkid).name], "candidates": names("saving")}, retrieval="all")
c = [(max(rates(p, 12, "rate")), p) for p in sv if rates(p, 12, "rate")]
top = argbest(c)[0]
add("N18", "hard", "calc", "은행 적금 중 12개월 기본금리 제일 높은 상품에 월 50만원씩 넣으면 세전이자 얼마야?", argbest(c),
    {"type": "nums_any", "values": [int(500_000 * 78 * max(rates(top, 12, "rate")) / 1200)]})
text_has(k26, "spcl_cnd", "성공으로 인정되지 않음")
items.append({"id": "N19", "difficulty": "easy", "category": "follow_up", "setup": "카카오뱅크 26주적금 우대금리 알려줘",
              "query": "그거 자동이체 실패한 주차를 빈자리채우기로 메우면 성공으로 쳐줘?", "gold_ids": [k26.id],
              "retrieval": "any", "check": {"type": "text_any", "values": ["인정되지 않", "인정되지않", "인정하지 않", "아니요"]}})
r24 = rates(woori, 24, "rate"); assert len(r24) == 1
items.append({"id": "N20", "difficulty": "easy", "category": "follow_up", "setup": "우리SUPER주거래적금 12개월 기본금리 얼마야?",
              "query": "그거 24개월은?", "gold_ids": [woori.id], "retrieval": "any",
              "check": {"type": "nums_any", "values": sorted(r24)}})

# ---- 잡담·범위 밖 ----
add("C1", "easy", "chat", "안녕! 너는 뭘 도와줄 수 있어?", [], {"type": "nonempty"})
add("C2", "easy", "out_of_scope", "오늘 삼성전자 주가 얼마야?", [], {"type": "refusal"})

out = ROOT / "eval/golden.jsonl"
out.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
print(f"{len(items)}문항 -> {out.relative_to(ROOT)}")
for i in items:
    print(f"  {i['id']:3} {i['difficulty']:4} {i['category']:12} {i['check']['type']:10} gold={i['check'].get('values', '-')}")
