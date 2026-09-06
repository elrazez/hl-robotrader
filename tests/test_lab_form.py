"""The form the lab builds from the Settings model.

The point of these tests is that the form is derived, never listed: a knob added to
`config/models.py` has to appear in the lab with no lab change at all.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from liqfade.lab.form import LAB_SECTIONS, lab_fields, walk


class Inner(BaseModel):
    ratio: float = Field(0.5, description="fraction: how much")
    mode: Literal["fast", "slow"] = Field("fast", description="which way")


class Outer(BaseModel):
    inner: Inner
    count: int = Field(3, description="count: how many")


def test_walk_descends_into_nested_models_and_keeps_the_dotted_key() -> None:
    keys = [field.key for field in walk(Outer)]
    assert keys == ["inner.ratio", "inner.mode", "count"]


def test_walk_carries_the_models_description_and_default() -> None:
    ratio = next(f for f in walk(Outer) if f.key == "inner.ratio")
    assert ratio.description == "fraction: how much"
    assert ratio.default == 0.5
    assert ratio.kind == "float"


def test_a_literal_becomes_a_choice_field() -> None:
    mode = next(f for f in walk(Outer) if f.key == "inner.mode")
    assert mode.kind == "choice"
    assert mode.choices == ("fast", "slow")


def test_a_field_added_to_a_model_appears_with_no_lab_change() -> None:
    """The pin the spec asks for, on a model the lab has never heard of."""

    class Grown(Outer):
        new_knob: int = Field(7, description="count: added after the lab was written")

    assert "new_knob" in [field.key for field in walk(Grown)]


def test_lab_fields_covers_the_sections_a_backtest_reads() -> None:
    keys = {field.key for field in lab_fields()}
    for section in LAB_SECTIONS:
        assert any(key.startswith(f"{section}.") for key in keys), section
    # A knob from each layer the owner tunes, including the newest rule.
    for key in (
        "cascade.liq_usd_min.core",
        "exhaustion.mode",
        "intent.k_target",
        "strategy.rules.level_reclaim.sweep_min_bps",
        "strategy.min_confluence",
        "sizing.risk_frac",
        "risk.max_dd_frac",
        "backtest.latency_ms",
        "filters.min_edge_multiple",
        "features.z_lookback_h",
    ):
        assert key in keys, key
    # Nothing outside those sections: the lab does not edit venues, keys or paths.
    assert not any(key.startswith(("venues.", "paths.", "telegram.", "panel.")) for key in keys)


def test_lab_fields_defaults_come_from_the_resolved_config_not_the_model() -> None:
    """`config/default.yaml` is what a backtest starts from, so it is what the form shows."""
    latency = next(f for f in lab_fields() if f.key == "backtest.latency_ms")
    assert latency.default == 1000


def test_typing_a_value_parses_it_the_way_yaml_would() -> None:
    from liqfade.lab.form import parse_value

    fields = {field.key: field for field in lab_fields()}
    assert parse_value(fields["backtest.latency_ms"], "1500") == 1500
    assert parse_value(fields["sizing.risk_frac"], "0.004") == 0.004
    assert parse_value(fields["filters.skip_when_btc_cascading"], "false") is False
    assert parse_value(fields["exhaustion.mode"], "rule") == "rule"
    assert parse_value(fields["features.windows_s"], "30, 60, 300") == [30, 60, 300]


def test_an_empty_box_is_not_a_value_and_null_has_to_be_typed() -> None:
    """An empty box means the key is absent from the layer file, which is not the same as
    setting it to null: `cascade.liq_z_min: null` is a real override the owner writes."""
    from liqfade.lab.form import LabValueError, parse_value

    fields = {field.key: field for field in lab_fields()}
    assert parse_value(fields["cascade.liq_z_min"], "null") is None
    try:
        parse_value(fields["cascade.liq_z_min"], "")
    except LabValueError as exc:
        assert "null" in str(exc)
    else:
        raise AssertionError("an empty box is not a value")


def test_validation_uses_the_models_own_message_and_names_the_key() -> None:
    from liqfade.lab.form import validate

    assert validate({}) == {}
    errors = validate({"sizing.risk_frac": 2.0})
    assert "sizing.risk_frac" in errors
    assert "less than or equal to 1" in errors["sizing.risk_frac"]


def test_a_cross_field_validator_reports_against_its_section() -> None:
    from liqfade.lab.form import validate

    errors = validate({"exhaustion.min_age_s": 900.0, "exhaustion.max_age_s": 5.0})
    assert "max_age_s must be > min_age_s" in errors["exhaustion"]


def test_an_unknown_key_is_refused_rather_than_silently_ignored() -> None:
    from liqfade.lab.form import validate

    assert "cascade.liq_z_mim" in validate({"cascade.liq_z_mim": 2.0})
