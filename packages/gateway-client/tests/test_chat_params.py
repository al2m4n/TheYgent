"""The chat-param split: OpenAI-standard fields stay named kwargs; anything else must ride
``extra_body`` (the SDK's typed ``create`` rejects unknown kwargs with a TypeError, so an
engine knob such as ``chat_template_kwargs`` would otherwise fail the whole call)."""

from theygent_gateway_client.client import _chat_kwargs


def test_standard_params_stay_top_level() -> None:
    out = _chat_kwargs({"temperature": 0.2, "max_tokens": 128, "stop": ["END"]})
    assert out == {"temperature": 0.2, "max_tokens": 128, "stop": ["END"]}
    assert "extra_body" not in out


def test_engine_knobs_route_through_extra_body() -> None:
    out = _chat_kwargs({"temperature": 0.2, "chat_template_kwargs": {"enable_thinking": False}})
    assert out["temperature"] == 0.2
    assert out["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "chat_template_kwargs" not in out


def test_caller_supplied_extra_body_merges_and_wins() -> None:
    out = _chat_kwargs(
        {
            "top_k": 40,
            "extra_body": {"top_k": 20, "min_p": 0.05},
        }
    )
    assert out["extra_body"] == {"top_k": 20, "min_p": 0.05}


def test_reserved_and_empty() -> None:
    assert _chat_kwargs(None) == {}
    assert _chat_kwargs({"model": "x", "messages": [], "stream": True}) == {}
