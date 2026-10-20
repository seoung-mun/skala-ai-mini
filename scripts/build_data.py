"""data/raw/*.json -> data/md/{kind}/{group}/{id}.md. 실행할 때마다 이전에 생성한 .md를 지우고 다시 만든다."""
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from finagent.parser import parse_all, to_markdown


def main() -> None:
    products = parse_all(ROOT / "data/raw")
    md_dir = ROOT / "data/md"
    for old in md_dir.rglob("*.md"):  # 이 스크립트의 생성물만 지운다(사라진 상품이 남지 않게)
        old.unlink()
    n = Counter()
    for p in products:
        out = ROOT / "data/md" / p.kind / p.group / f"{p.id}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(to_markdown(p), encoding="utf-8")
        n[(p.kind, p.group)] += 1
    for (kind, group), c in sorted(n.items()):
        print(f"{kind:9} {group:12} {c:4}")
    flagged = [p for p in products if p.flags]
    print(f"합계 {len(products)}개, 데이터 주의 {len(flagged)}개, 금리 옵션 없음 {sum(1 for p in products if not p.options)}개")


if __name__ == "__main__":
    main()
