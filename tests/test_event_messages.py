"""Parsing PullMessages notifications, with the fake responder's fixtures."""

from nightwire.events import messages
from tests import fake_pullpoint as fake

T0 = 1_793_000_000_000


def test_a_motion_notification_parses():
    parsed = messages.parse(fake.motion(T0, True))
    assert parsed.topic == fake.VMD_TOPIC
    assert parsed.value is True
    assert parsed.is_stateful
    assert parsed.source_items["ProfileID"] == "1"
    # Within a millisecond: the fixture formats microseconds.
    assert abs(parsed.camera_ts_ms - T0) <= 1


def test_false_parses_as_a_close():
    assert messages.parse(fake.motion(T0, False)).value is False


def test_a_one_shot_notification_has_no_state():
    parsed = messages.parse(fake.crossing(T0))
    assert parsed.value is None
    assert not parsed.is_stateful
    assert parsed.source_items["LineID"] == "1"


def test_junk_is_rejected_rather_than_half_parsed():
    for junk in [None, {}, "text", {"Message": {}}, {"Topic": {}}]:
        assert messages.parse(junk) is None


def test_a_notification_with_no_message_still_parses_its_topic():
    parsed = messages.parse({"Topic": {"_value_1": "a/b"}})
    assert parsed.topic == "a/b"
    assert parsed.value is None
    assert parsed.camera_ts_ms is None


def test_topic_normalisation():
    assert messages.normalise_topic({"_value_1": " a/b "}) == "a/b"
    assert messages.normalise_topic("/a/b") == "a/b"
    assert messages.normalise_topic({"nested": {"_value_1": "a/b"}}) == "a/b"
    assert messages.normalise_topic(None) == ""


def test_value_interpretation_reads_values_not_field_names():
    assert messages.interpret_value({"anything": "true"}) is True
    assert messages.interpret_value({"whatever": "false"}) is False
    assert messages.interpret_value({"state": "ACTIVE"}) is True
    assert messages.interpret_value({"x": "inactive"}) is False


def test_a_bare_number_is_only_a_boolean_on_a_state_like_field():
    """`Count: "1"` on a line crossing means one crossing, not "on"."""
    assert messages.interpret_value({"Count": "1"}) is None
    assert messages.interpret_value({"Index": "0"}) is None
    assert messages.interpret_value({"active": "1"}) is True
    assert messages.interpret_value({"logicalState": "0"}) is False


def test_a_message_with_no_boolean_is_a_one_shot():
    assert messages.interpret_value({"Count": "3"}) is None
    assert messages.interpret_value({}) is None


def test_a_state_like_field_wins_when_several_booleans_are_present():
    assert messages.interpret_value({"logicalState": "true", "other": "false"}) is True
    assert messages.interpret_value({"active": "false", "other": "true"}) is False


def test_utc_time_is_read_from_an_attribute_dict():
    raw = {"Topic": {"_value_1": "a/b"}, "Message": {"Message": {"_attr_1": {"UtcTime": fake.iso(T0)}}}}
    assert abs(messages.parse(raw).camera_ts_ms - T0) <= 1


def test_a_singly_wrapped_message_also_parses():
    raw = {"Topic": {"_value_1": "a/b"}, "Message": {"UtcTime": fake.iso(T0), "Data": {"SimpleItem": {"Name": "x", "Value": "true"}}}}
    parsed = messages.parse(raw)
    assert parsed.value is True


def index_for(topic, source_filter, source_id=1, kind="span"):
    from nightwire.events import topics as topics_mod

    return {
        topics_mod.source_key(topic, source_filter): {
            "id": source_id,
            "topic": topic,
            "source_filter": source_filter,
            "kind": kind,
        }
    }


def test_matching_prefers_the_exact_source():
    parsed = messages.parse(fake.motion(T0, True, profile="2"))
    index = index_for(fake.VMD_TOPIC, "ProfileID=2,VideoSourceConfigurationToken=0")
    assert messages.match(parsed, index)["id"] == 1


def test_matching_falls_back_to_a_single_source_field():
    """Two motion profiles on one camera become two lanes this way."""
    parsed = messages.parse(fake.motion(T0, True, profile="2"))
    index = index_for(fake.VMD_TOPIC, "ProfileID=2")
    assert messages.match(parsed, index)["id"] == 1


def test_matching_falls_back_to_the_topic_alone():
    parsed = messages.parse(fake.motion(T0, True))
    index = index_for(fake.VMD_TOPIC, None)
    assert messages.match(parsed, index)["id"] == 1


def test_the_wrong_profile_does_not_match():
    parsed = messages.parse(fake.motion(T0, True, profile="1"))
    index = index_for(fake.VMD_TOPIC, "ProfileID=2")
    assert messages.match(parsed, index) is None


def test_an_unconfigured_topic_does_not_match():
    parsed = messages.parse(fake.crossing(T0))
    index = index_for(fake.VMD_TOPIC, None)
    assert messages.match(parsed, index) is None
