"""Topic discovery. The spec forbids hard-coding vendor topic strings."""

from nightwire.events import topics
from tests import fake_pullpoint as fake


def test_flatten_finds_every_topic_leaf():
    found = topics.flatten(fake.topic_set())
    paths = [t.path for t in found]
    assert len(found) == 3
    assert "tns1:RuleEngine/tnsaxis:VMD3/vmd3_video_1" in paths
    assert "tns1:RuleEngine/tnsaxis:FenceGuard/Crossed" in paths
    assert "tns1:VideoSource/tnsaxis:Tampering" in paths


def test_flatten_walks_containers_that_are_not_topics():
    """`tns1:RuleEngine` has no MessageDescription; it is not a topic."""
    assert "tns1:RuleEngine" not in [t.path for t in topics.flatten(fake.topic_set())]


def test_is_property_decides_span_versus_point():
    """This is the whole reason no vendor string is needed."""
    by_path = {t.path: t for t in topics.flatten(fake.topic_set())}
    assert by_path["tns1:RuleEngine/tnsaxis:VMD3/vmd3_video_1"].kind == topics.SPAN
    assert by_path["tns1:RuleEngine/tnsaxis:FenceGuard/Crossed"].kind == topics.POINT
    assert by_path["tns1:VideoSource/tnsaxis:Tampering"].kind == topics.SPAN


def test_is_property_accepts_the_shapes_zeep_produces():
    for description in [
        {"IsProperty": "true"},
        {"IsProperty": True},
        {"isProperty": "1"},
        {"_attr_1": {"IsProperty": "true"}},
    ]:
        [found] = topics.flatten({"T": {"MessageDescription": description}})
        assert found.is_property is True, description
    for description in [{}, {"IsProperty": "false"}, {"IsProperty": 0}]:
        [found] = topics.flatten({"T": {"MessageDescription": description}})
        assert found.is_property is False, description


def test_source_and_data_field_names_are_captured():
    by_path = {t.path: t for t in topics.flatten(fake.topic_set())}
    vmd = by_path["tns1:RuleEngine/tnsaxis:VMD3/vmd3_video_1"]
    assert vmd.source_names == ("VideoSourceConfigurationToken", "ProfileID")
    assert vmd.data_names == ("active",)


def test_a_topic_with_no_source_fields():
    by_path = {t.path: t for t in topics.flatten(fake.topic_set())}
    assert by_path["tns1:VideoSource/tnsaxis:Tampering"].source_names == ()


def test_flatten_tolerates_junk():
    for junk in [None, {}, "text", 5, {"T": "not a dict"}]:
        assert topics.flatten(junk) == []


def test_flatten_ignores_metadata_keys():
    tree = {"_value_1": {"MessageDescription": {}}, "Real": {"MessageDescription": {}}}
    assert [t.path for t in topics.flatten(tree)] == ["Real"]


def test_flatten_is_depth_limited():
    """A cyclic or absurdly deep tree must not blow the stack."""
    deep = current = {}
    for _ in range(40):
        nested = {}
        current["Level"] = nested
        current = nested
    current["MessageDescription"] = {}
    assert topics.flatten(deep) == []


def test_labels_are_derived_not_looked_up():
    assert topics.default_label("tns1:RuleEngine/tnsaxis:VMD3/vmd3_video_1") == "VMD3 vmd3 video 1"
    assert topics.default_label("tns1:VideoSource/MotionAlarm") == "Video Source Motion Alarm"
    assert topics.default_label("tns1:RuleEngine/FenceGuard/Crossed") == "Fence Guard Crossed"


def test_a_label_for_a_topic_nobody_has_seen():
    assert topics.default_label("vendorx:SomeNewThing/Triggered") == "Some New Thing Triggered"


def test_a_repeated_tail_is_not_doubled():
    assert topics.default_label("tns1:RuleEngine/Motion/Motion") == "Motion"


def test_label_of_an_empty_path():
    assert topics.default_label("") == "Event"
    assert topics.default_label(None) == "Event"


def test_strip_prefix_and_split_words():
    assert topics.strip_prefix("tnsaxis:VMD3") == "VMD3"
    assert topics.strip_prefix("Plain") == "Plain"
    assert topics.split_words("VideoMotionDetection") == "Video Motion Detection"
    assert topics.split_words("vmd3_video_1") == "vmd3 video 1"


def test_shows_as_matches_the_nodes_column():
    [span] = topics.flatten({"T": {"MessageDescription": {"IsProperty": "true"}}})
    [point] = topics.flatten({"T": {"MessageDescription": {}}})
    assert span.describes_as == "Block (on/off)"
    assert point.describes_as == "Diamond (one-off)"


def test_source_key_identifies_one_source():
    assert topics.source_key("topic", "ProfileID=1") == "topic#ProfileID=1"
    assert topics.source_key("topic", None) == "topic#"


def test_message_source_filter_is_stable_regardless_of_key_order():
    one = topics.message_source_filter({"ProfileID": "1", "Token": "0"})
    two = topics.message_source_filter({"Token": "0", "ProfileID": "1"})
    assert one == two == "ProfileID=1,Token=0"


def test_message_source_filter_can_be_narrowed_to_named_fields():
    items = {"ProfileID": "2", "VideoSourceConfigurationToken": "0"}
    assert topics.message_source_filter(items, ("ProfileID",)) == "ProfileID=2"


def test_message_source_filter_on_nothing():
    assert topics.message_source_filter(None) == ""
    assert topics.message_source_filter({}) == ""


def test_no_vendor_topic_string_is_hard_coded_in_the_module():
    """A guard, so a later change does not quietly reintroduce one.

    Docstrings are stripped first: explaining what `tnsaxis:VMD3` looks like
    is documentation, and matching on it is the thing that is forbidden.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(topics.__file__).read_text())
    code_strings: list[str] = []
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                code_strings.append(node.value)

    joined = "\n".join(code_strings)
    for forbidden in ["tns1:", "tnsaxis:", "VideoMotionDetection", "FenceGuard", "VMD"]:
        assert forbidden not in joined, f"{forbidden!r} must not appear in code"
