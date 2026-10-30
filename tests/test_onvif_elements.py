"""Turning a camera's XML topic tree into the dicts the app walks.

GetEventProperties returns its TopicSet as xsd:any, which zeep hands back as
raw XML elements rather than parsed structures. `events/topics.flatten` walks
dicts and skips keys beginning with an underscore, so before this conversion
existed a real camera reported **zero event sources**: an Axis M1234-LVE
advertising 50 topics produced an empty list on 2026-10-30.
"""

from __future__ import annotations

from xml.etree import ElementTree

from nightwire.events import topics as topics_mod
from nightwire.onvif import client

TNS = "http://www.onvif.org/ver10/topics"
TT = "http://www.onvif.org/ver10/schema"

TOPIC_SET_XML = f"""
<TopicSet xmlns="{TNS}" xmlns:tt="{TT}">
  <VideoSource>
    <MotionAlarm wstop:topic="true" xmlns:wstop="http://docs.oasis-open.org/wsn/t-1">
      <tt:MessageDescription IsProperty="true">
        <tt:Source>
          <tt:SimpleItemDescription Name="Source" Type="tt:ReferenceToken"/>
        </tt:Source>
        <tt:Data>
          <tt:SimpleItemDescription Name="State" Type="xs:boolean"/>
        </tt:Data>
      </tt:MessageDescription>
    </MotionAlarm>
    <Tampering>
      <tt:MessageDescription>
        <tt:Source>
          <tt:SimpleItemDescription Name="channel" Type="xs:int"/>
        </tt:Source>
        <tt:Data>
          <tt:SimpleItemDescription Name="tampering" Type="xs:boolean"/>
        </tt:Data>
      </tt:MessageDescription>
    </Tampering>
  </VideoSource>
</TopicSet>
"""


def element():
    return ElementTree.fromstring(TOPIC_SET_XML)


def test_namespaces_are_dropped_from_names():
    assert client.local_name("{http://www.onvif.org/ver10/topics}VideoSource") == "VideoSource"
    assert client.local_name("VideoSource") == "VideoSource"


def test_an_element_becomes_nested_dicts():
    converted = client.element_to_dict(element())
    assert "VideoSource" in converted
    assert set(converted["VideoSource"]) >= {"MotionAlarm", "Tampering"}


def test_attributes_survive_the_conversion():
    converted = client.element_to_dict(element())
    description = converted["VideoSource"]["MotionAlarm"]["MessageDescription"]
    assert description["_attr_1"]["IsProperty"] == "true"
    assert description["IsProperty"] == "true"


def test_the_topic_walker_finds_the_topics():
    """The assertion that matters: 50 of these were being thrown away."""
    converted = client.element_to_dict(element())
    found = topics_mod.flatten(converted)
    paths = [topic.path for topic in found]
    assert "VideoSource/MotionAlarm" in paths
    assert "VideoSource/Tampering" in paths


def test_source_and_data_names_survive():
    found = {t.path: t for t in topics_mod.flatten(client.element_to_dict(element()))}
    motion = found["VideoSource/MotionAlarm"]
    assert motion.source_names == ("Source",)
    assert motion.data_names == ("State",)
    assert motion.is_property is True
    assert found["VideoSource/Tampering"].is_property is False


def test_repeated_children_become_a_list():
    xml = "<a><b><c Name='one'/><c Name='two'/></b></a>"
    converted = client.element_to_dict(ElementTree.fromstring(xml))
    assert isinstance(converted["b"]["c"], list)
    assert [item["Name"] for item in converted["b"]["c"]] == ["one", "two"]


def test_conversion_is_depth_limited():
    """A malformed or hostile document must not recurse without bound."""
    deep = "".join(f"<n{i}>" for i in range(60)) + "".join(f"</n{i}>" for i in reversed(range(60)))
    converted = client.element_to_dict(ElementTree.fromstring(f"<root>{deep}</root>"))
    assert isinstance(converted, dict)


def test_any_content_is_lifted_under_its_own_tag_name():
    """zeep parks the whole TopicSet under `_value_1` as raw elements."""
    root = element()
    pretend_zeep = {"documentation": None, "_value_1": list(root), "_attr_1": None}
    converted = client._as_dict(pretend_zeep)
    assert "VideoSource" in converted
    assert topics_mod.flatten(converted)


def test_text_only_elements_keep_their_text():
    converted = client.element_to_dict(ElementTree.fromstring("<a><b>hello</b></a>"))
    assert converted["b"]["_value_1"] == "hello"
