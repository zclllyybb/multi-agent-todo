from core.model_config import (
    ModelSpec,
    model_spec_list_to_config_value,
    model_spec_map_to_config_value,
    model_spec_to_config_value,
    parse_model_spec,
    parse_model_spec_list,
    parse_model_spec_map,
)


def test_parse_model_spec_from_string():
    assert parse_model_spec("gpt-5") == ModelSpec(model="gpt-5", variant="")


def test_parse_model_spec_from_dict():
    assert parse_model_spec({"model": "gpt-5", "variant": "fast"}) == ModelSpec(
        model="gpt-5",
        variant="fast",
    )


def test_parse_model_spec_map_and_list_support_mixed_legacy_and_structured_values():
    assert parse_model_spec_map(
        {"simple": "a", "complex": {"model": "b", "variant": "v"}}
    ) == {
        "simple": ModelSpec(model="a", variant=""),
        "complex": ModelSpec(model="b", variant="v"),
    }
    assert parse_model_spec_list(["a", {"model": "b", "variant": "v"}]) == [
        ModelSpec(model="a", variant=""),
        ModelSpec(model="b", variant="v"),
    ]


def test_model_spec_to_config_helpers_round_trip():
    assert model_spec_to_config_value(ModelSpec(model="a", variant="")) == "a"
    assert model_spec_to_config_value(ModelSpec(model="a", variant="v")) == {
        "model": "a",
        "variant": "v",
    }
    assert model_spec_map_to_config_value(
        {"simple": ModelSpec(model="a", variant="v")}
    ) == {"simple": {"model": "a", "variant": "v"}}
    assert model_spec_list_to_config_value(
        [ModelSpec(model="a", variant="v"), ModelSpec(model="b", variant="")]
    ) == [
        {"model": "a", "variant": "v"},
        "b",
    ]


def test_parse_model_spec_accepts_missing_variant_key():
    assert parse_model_spec({"model": "gpt-5"}) == ModelSpec(model="gpt-5", variant="")
