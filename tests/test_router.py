"""Milestone 2: the model router and the monthly budget (R1 to R5)."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tanios.catalog import parse_catalog
from tanios.llm import ModelError, ModelReply
from tanios.router import (
    CHAT,
    BudgetExceeded,
    NoModelAvailable,
    Router,
    Task,
    money,
    start_of_month,
    start_of_next_month,
)
from tests.fakes import NOW, OWNER, FakeGateway, InMemoryStore, make_router

SORT = Task("sort")  # starts on the free tier
DRAFT = Task("draft", private=True)  # starts on the medium tier, private content
PLAN = Task("plan")  # starts on the top tier
PRIVATE_SORT = Task("sort", private=True)


class Bench:
    def __init__(self, gateway: FakeGateway | None = None, **user) -> None:
        self.store = InMemoryStore()
        self.user_id = self.store.add_user(OWNER, **user)
        self.gateway = gateway or FakeGateway()
        self.router = make_router(self.store, self.gateway)

    def ask(self, task: Task = CHAT, prompt: str = "question", router: Router | None = None, **kw):
        user = self.store.user(self.user_id)
        return asyncio.run(
            (router or self.router).complete(
                user=user, task=task, system="Be brief.", prompt=prompt, **kw
            )
        )

    @property
    def ledger(self) -> list[tuple[str, str, str, str]]:
        return [(e.task, e.tier, e.model, e.outcome) for e in self.store.ledger]


# R1: every call goes through the router, which picks a tier by task type ---------------


@pytest.mark.parametrize(
    ("task", "tier", "model"),
    [
        (SORT, "free", "free-model"),
        (CHAT, "small", "small-model"),
        (DRAFT, "medium", "medium-model"),
        (PLAN, "top", "top-model"),
    ],
)
def test_r1_each_task_type_starts_on_its_own_tier(task, tier, model):
    bench = Bench()

    reply = bench.ask(task)

    assert (reply.tier, reply.model) == (tier, model)
    assert bench.gateway.models == [model]


def test_r1_every_call_is_written_to_the_ledger_with_task_model_and_cost():
    bench = Bench(FakeGateway(cost=Decimal("0.0123")))

    bench.ask(CHAT)

    [entry] = bench.store.ledger
    assert (entry.task, entry.tier, entry.provider, entry.model) == (
        "chat",
        "small",
        "openrouter",
        "small-model",
    )
    assert entry.cost_usd == Decimal("0.0123")
    assert (entry.prompt_tokens, entry.completion_tokens) == (100, 50)
    assert entry.outcome == "ok"
    assert entry.at == NOW


def test_r1_the_log_names_the_model_that_handled_the_task_but_never_the_text(caplog):
    bench = Bench(FakeGateway(answer="a private answer"))

    with caplog.at_level("INFO"):
        bench.ask(CHAT, prompt="a private question")

    assert "task=chat tier=small model=small-model" in caplog.text
    assert "private" not in caplog.text


def test_r1_within_a_tier_the_first_listed_model_is_used():
    bench = Bench()
    router = make_router(
        bench.store, bench.gateway, keys={"openrouter": "sk-or", "other": "sk-other"}
    )

    bench.ask(CHAT, router=router)

    assert bench.gateway.models == ["small-model"]


def test_r1_a_task_with_no_tier_in_the_settings_file_is_a_clear_error():
    bench = Bench()

    with pytest.raises(Exception, match="no tier is set for the task 'unknown'"):
        bench.ask(Task("unknown"))


# R2: one OpenRouter key, plus keys for other OpenAI-compatible providers ---------------


def test_r2_one_openrouter_key_is_enough():
    bench = Bench()

    assert bench.ask(CHAT).text == "Here is your answer."
    assert bench.gateway.calls[0]["provider"] == "openrouter"
    assert bench.gateway.calls[0]["api_key"] == "sk-or-test"


def test_r2_a_second_provider_is_used_with_its_own_key_when_openrouter_has_none():
    bench = Bench()
    router = make_router(bench.store, bench.gateway, keys={"other": "sk-other"})

    reply = bench.ask(CHAT, router=router)

    assert reply.model == "other-small-model"
    assert bench.gateway.calls[0]["provider"] == "other"
    assert bench.gateway.calls[0]["api_key"] == "sk-other"


def test_r2_key_names_lists_providers_with_a_key_and_ignores_unknown_ones():
    bench = Bench()
    router = make_router(
        bench.store,
        bench.gateway,
        keys={"other": "sk-other", "openrouter": "sk-or", "not-in-catalog": "x", "blank": ""},
    )

    assert router.key_names() == ["openrouter", "other"]


def test_r2_with_no_usable_key_there_is_no_model():
    bench = Bench()
    router = make_router(bench.store, bench.gateway, keys={})

    assert router.can_answer(CHAT) is False
    with pytest.raises(NoModelAvailable):
        bench.ask(CHAT, router=router)
    assert bench.gateway.calls == []


# R3: private content stays away from free models ---------------------------------------


def test_r3_a_private_task_skips_the_free_model_and_uses_the_next_tier():
    bench = Bench()

    reply = bench.ask(PRIVATE_SORT)

    assert reply.model == "small-model"
    assert "free-model" not in bench.gateway.models
    assert all(not entry.is_free for entry in bench.store.ledger)
    assert all(entry.private for entry in bench.store.ledger)


def test_r3_the_same_task_without_private_content_uses_the_free_model():
    bench = Bench()

    assert bench.ask(SORT).model == "free-model"
    assert bench.store.ledger[0].is_free is True


def test_r3_the_user_can_switch_free_models_on_for_private_content():
    bench = Bench(allow_free_for_private=True)

    assert bench.ask(PRIVATE_SORT).model == "free-model"


def test_r3_over_the_cap_private_content_still_never_reaches_a_free_model():
    bench = Bench(FakeGateway(cost=Decimal("2")), monthly_cap_usd="1")
    bench.ask(CHAT)  # spends 2, over the cap

    with pytest.raises(BudgetExceeded):
        bench.ask(DRAFT)

    assert "free-model" not in bench.gateway.models


def test_r3_a_retry_after_a_failed_check_also_respects_privacy():
    # The private task starts on medium; nothing about a retry can send it down to free.
    bench = Bench(FakeGateway(by_model={"medium-model": ""}))

    reply = bench.ask(DRAFT)

    assert bench.gateway.models == ["medium-model", "top-model"]
    assert reply.model == "top-model"


# R4: the monthly cap -------------------------------------------------------------------


def test_r4_no_warning_below_80_percent():
    bench = Bench(FakeGateway(cost=Decimal("0.79")), monthly_cap_usd="1")

    assert bench.ask(CHAT).notice is None


def test_r4_a_warning_at_80_percent_and_only_once_in_the_month():
    bench = Bench(FakeGateway(cost=Decimal("0.40")), monthly_cap_usd="1")

    first = bench.ask(CHAT)  # 0.40
    second = bench.ask(CHAT)  # 0.80: exactly 80%
    third = bench.ask(CHAT, router=make_router(bench.store, FakeGateway(cost=Decimal("0.05"))))

    assert first.notice is None
    assert second.notice == "Heads up: you have used $0.80 of your $1.00 monthly cap (80%)."
    assert third.notice is None  # 0.85, already warned this month


def test_r4_at_100_percent_the_user_is_told_and_paid_calls_stop():
    bench = Bench(FakeGateway(cost=Decimal("0.60")), monthly_cap_usd="1")

    bench.ask(CHAT)  # 0.60
    reached = bench.ask(CHAT)  # 1.20
    after = bench.ask(CHAT)  # paid models are off now

    assert "You have reached your monthly cap of $1.00." in reached.notice
    assert "Paid models are off until 1 December." in reached.notice
    assert "free models" in reached.notice
    assert after.model == "free-model"
    assert after.notice is None  # told once
    assert bench.gateway.models == ["small-model", "small-model", "free-model"]


def test_r4_once_over_the_cap_no_paid_model_is_called_however_often_the_user_asks():
    bench = Bench(FakeGateway(cost=Decimal("5")), monthly_cap_usd="1")
    bench.ask(CHAT)

    for _ in range(5):
        bench.ask(PLAN)

    assert bench.gateway.models == ["small-model"] + ["free-model"] * 5
    assert asyncio.run(bench.store.spend_since(bench.user_id, start_of_month(NOW))) == 5


def test_r4_with_no_free_model_the_user_gets_a_clear_stop_message():
    catalog = parse_catalog(
        {
            "providers": {"openrouter": {"api_base": "https://models.example/api/v1"}},
            "tiers": {"small": [{"provider": "openrouter", "model": "small-model"}]},
            "tasks": {"chat": "small"},
        }
    )
    bench = Bench(FakeGateway(cost=Decimal("1")), monthly_cap_usd="1")
    router = make_router(bench.store, bench.gateway, catalog=catalog)

    reached = bench.ask(CHAT, router=router)
    assert "cannot answer questions until then" in reached.notice

    with pytest.raises(BudgetExceeded) as stopped:
        bench.ask(CHAT, router=router)

    assert "You have reached your monthly cap of $1.00." in str(stopped.value)
    assert "Commands still work" in str(stopped.value)
    assert len(bench.gateway.calls) == 1


def test_r4_the_cap_starts_fresh_in_a_new_month():
    bench = Bench(FakeGateway(cost=Decimal("2")), monthly_cap_usd="1")
    bench.ask(CHAT)  # November: over the cap

    december = make_router(
        bench.store,
        FakeGateway(cost=Decimal("0.10")),
        now=datetime(2026, 12, 1, 0, 5, tzinfo=UTC),
    )
    reply = bench.ask(CHAT, router=december)

    assert reply.model == "small-model"
    assert reply.notice is None


def test_r4_a_user_with_no_cap_is_never_warned_or_stopped():
    bench = Bench(FakeGateway(cost=Decimal("500")))

    assert bench.ask(CHAT).notice is None
    assert bench.ask(CHAT).model == "small-model"


def test_r4_a_cap_of_zero_means_free_models_only():
    bench = Bench(monthly_cap_usd="0")

    assert bench.ask(CHAT).model == "free-model"


def test_r4_one_users_spending_does_not_count_against_another():
    bench = Bench(FakeGateway(cost=Decimal("5")), monthly_cap_usd="1")
    other_id = bench.store.add_user("15550001111", monthly_cap_usd="1")
    bench.ask(CHAT)  # the first user is now over the cap

    other = asyncio.run(
        bench.router.complete(
            user=bench.store.user(other_id), task=CHAT, system="s", prompt="question"
        )
    )

    assert other.model == "small-model"


def test_r4_cost_comes_from_listed_prices_when_the_provider_reports_none():
    catalog = parse_catalog(
        {
            "providers": {"openrouter": {"api_base": "https://models.example/api/v1"}},
            "tiers": {
                "small": [
                    {
                        "provider": "openrouter",
                        "model": "priced-model",
                        "input_price": 2.0,
                        "output_price": 10.0,
                    }
                ]
            },
            "tasks": {"chat": "small"},
        }
    )

    class NoCostGateway(FakeGateway):
        async def call(self, **kwargs) -> ModelReply:
            return ModelReply(
                text="ok",
                model="priced-model",
                cost_usd=None,
                prompt_tokens=1000,
                completion_tokens=500,
            )

    bench = Bench()
    bench.ask(CHAT, router=make_router(bench.store, NoCostGateway(), catalog=catalog))

    # 1000 x $2/M + 500 x $10/M
    assert bench.store.ledger[0].cost_usd == Decimal("0.007")


# R5: one retry on the next tier up, never more -----------------------------------------


def test_r5_an_answer_that_fails_its_check_is_retried_once_on_the_next_tier_up():
    bench = Bench(FakeGateway(by_model={"small-model": "  "}))

    reply = bench.ask(CHAT)

    assert bench.gateway.models == ["small-model", "medium-model"]
    assert reply.model == "medium-model"
    assert reply.tier == "medium"
    assert reply.retried is True
    assert bench.ledger == [
        ("chat", "small", "small-model", "failed_check"),
        ("chat", "medium", "medium-model", "ok"),
    ]


def test_r5_a_forced_bad_answer_triggers_exactly_one_retry_even_if_that_fails_too():
    bench = Bench(FakeGateway(answer=""))  # every model answers with nothing

    with pytest.raises(ModelError, match="after one retry"):
        bench.ask(CHAT)

    assert bench.gateway.models == ["small-model", "medium-model"]
    assert len(bench.store.ledger) == 2


def test_r5_a_good_first_answer_is_not_retried():
    bench = Bench()

    reply = bench.ask(CHAT)

    assert reply.retried is False
    assert len(bench.gateway.calls) == 1


def test_r5_a_task_can_bring_its_own_check():
    bench = Bench(FakeGateway(by_model={"small-model": "not a label", "medium-model": "FYI"}))

    reply = bench.ask(CHAT, check=lambda text: text in {"Needs reply", "FYI", "Newsletter"})

    assert reply.text == "FYI"
    assert bench.gateway.models == ["small-model", "medium-model"]


def test_r5_a_provider_error_also_gets_the_one_retry():
    bench = Bench(FakeGateway(by_model={"small-model": ModelError("HTTP 503")}))

    reply = bench.ask(CHAT)

    assert reply.model == "medium-model"
    assert bench.ledger[0] == ("chat", "small", "small-model", "error")
    assert bench.store.ledger[0].cost_usd == 0


def test_r5_the_cost_of_the_failed_first_answer_still_counts():
    bench = Bench(FakeGateway(by_model={"small-model": ""}, cost=Decimal("0.02")))

    bench.ask(CHAT)

    assert asyncio.run(bench.store.spend_since(bench.user_id, start_of_month(NOW))) == Decimal(
        "0.04"
    )


def test_r5_at_the_top_tier_there_is_nowhere_to_retry():
    bench = Bench(FakeGateway(by_model={"top-model": ""}))

    with pytest.raises(ModelError, match="no higher tier"):
        bench.ask(PLAN)

    assert bench.gateway.models == ["top-model"]


# Small helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "text"),
    [("0", "$0.00"), ("0.0042", "$0.0042"), ("0.1", "$0.10"), ("4.5", "$4.50"), ("12", "$12.00")],
)
def test_money_is_readable_for_small_and_large_amounts(amount, text):
    assert money(Decimal(amount)) == text


def test_month_boundaries_are_utc_calendar_months():
    moment = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)

    assert start_of_month(moment) == datetime(2026, 12, 1, tzinfo=UTC)
    assert start_of_next_month(moment) == datetime(2027, 1, 1, tzinfo=UTC)
