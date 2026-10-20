"""금감원 금융상품한눈에 5개 API 전 페이지 수집 -> data/raw/{endpoint}_{grp}.json. 키는 출력하지 않는다."""
import json, sys, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://finlife.fss.or.kr/finlifeapi"
ENDPOINTS = ["depositProductsSearch", "savingProductsSearch", "mortgageLoanProductsSearch",
             "rentHouseLoanProductsSearch", "creditLoanProductsSearch"]
GROUPS = {"020000": "bank", "030300": "savingsbank"}


def load_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "FINLIFE_API_KEY" and v.strip():
            return v.strip().strip('"').strip("'")
    raise SystemExit("FINLIFE_API_KEY가 .env에 없거나 비어 있습니다")


def fetch_page(client: httpx.Client, key: str, endpoint: str, grp: str, page: int) -> dict:
    try:
        r = client.get(f"{BASE}/{endpoint}.json", params={"auth": key, "topFinGrpNo": grp, "pageNo": page})
        r.raise_for_status()
    except httpx.HTTPError as e:  # 예외 메시지에 URL(키 포함)이 들어가므로 타입만 노출
        raise SystemExit(f"{endpoint} p{page} 요청 실패: {type(e).__name__}") from None
    res = r.json()["result"]
    if res["err_cd"] != "000":
        raise SystemExit(f"{endpoint} 응답 오류 {res['err_cd']}: {res['err_msg']}")
    return res


def main() -> None:
    key = load_key()
    print(f"key loaded (len={len(key)})")
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=60) as c:
        for ep in ENDPOINTS:
            for grp, gname in GROUPS.items():
                t = time.time()
                first = fetch_page(c, key, ep, grp, 1)
                base, opts = list(first.get("baseList", [])), list(first.get("optionList", []))
                for p in range(2, int(first["max_page_no"]) + 1):
                    nxt = fetch_page(c, key, ep, grp, p)
                    base += nxt.get("baseList", []); opts += nxt.get("optionList", [])
                (ROOT / "data/raw" / f"{ep}_{gname}.json").write_text(
                    json.dumps({"baseList": base, "optionList": opts}, ensure_ascii=False))
                print(f"{ep:30} {gname:12} total={first['total_count']:>4} pages={first['max_page_no']:>2} "
                      f"base={len(base):>4} options={len(opts):>5} {time.time()-t:.1f}s")


if __name__ == "__main__":
    main()
