from finagent.graph.build import next_slots


def test_answer_products_become_next_turn_candidates():
    state = {
        "slots": {"kinds": ["saving"], "product_ids": [], "rank": 1},
        "final": {"products": [{"id": "a"}, {"id": "b"}]},
    }
    assert next_slots(state) == {
        "kinds": ["saving"],
        "product_ids": ["a", "b"],
        "rank": 1,
    }


def test_no_products_keeps_route_slots():
    slots = {"kinds": ["deposit"], "product_ids": ["x"]}
    assert next_slots({"slots": slots, "final": {"products": []}}) == slots
    assert (
        next_slots({"slots": None, "final": {"products": [{"id": "a"}]}})
        is None
    )


def test_named_product_with_rank_queries_both_product_and_market_rank():
    import asyncio
    from types import SimpleNamespace

    from test_graph import CAT

    from finagent.graph.planning import plan_node
    from finagent.slots import Slots

    class Prompts:
        async def get_prompt(self, name, arguments):
            return "계획"

    pid = next(p.id for p in CAT.products if p.kind == "saving")
    slots = Slots(kinds=["saving"], product_ids=[pid], compare=True, rank=2)
    tasks = asyncio.run(
        plan_node(
            {"query": "그거랑 2위 비교", "slots": slots.to_dict()},
            SimpleNamespace(catalog=CAT, planner="rule", mcp=Prompts()),
        )
    )["tasks"]
    args = [t["args"] for t in tasks if t["agent"] == "structured"]
    assert {"product_ids": [pid]}.items() <= args[
        0
    ].items() and "rank" not in args[0]
    assert args[1]["rank"] == 2 and "product_ids" not in args[1]


def test_recall_prompt_lists_previous_questions_without_current_one():
    from finagent.schemas import ChatInput, Turn

    prompt = ChatInput(
        question="방금 내가 뭐 물어봤어?",
        history=[
            Turn(role="user", content="첫 질문"),
            Turn(role="assistant", content="답"),
            Turn(role="user", content="두번째 질문"),
            Turn(role="assistant", content="답"),
        ],
    ).to_prompt()
    listed = prompt.split("## 이번 질문")[0]
    assert "1. (직전) 두번째 질문" in listed and "2. 첫 질문" in listed
    assert "방금 내가" not in listed


def test_alternatives_exclude_products_already_shown():
    from test_graph import CAT

    top = CAT.query("saving", rank=1)
    shown = [r["product_id"] for r in top]
    rest = CAT.query("saving", exclude_ids=shown)
    assert rest and not {r["product_id"] for r in rest} & set(shown)
