"""The profile-selection logic, tested with Axis-shaped fixtures.

These dicts are the shape client.py flattens zeep objects into. Keeping the
logic pure is what makes this testable without the ONVIF stack installed.
"""

from nightwire.onvif import parse


def profile(token, name, codec="H264", width=1920, height=1080, fps=15, bitrate=4000):
    return {
        "token": token,
        "Name": name,
        "VideoEncoderConfiguration": {
            "Encoding": codec,
            "Resolution": {"Width": width, "Height": height},
            "RateControl": {"FrameRateLimit": fps, "BitrateLimit": bitrate},
        },
    }


AXIS_LIKE = [
    profile("profile_1", "nvr-main", width=1920, height=1080, fps=15),
    profile("profile_2", "nvr-sub", width=640, height=360, fps=10, bitrate=500),
    profile("profile_3", "Quality", codec="H265", width=2688, height=1512),
    profile("profile_4", "Mobile", width=480, height=270, fps=10, bitrate=300),
]


def test_normalise_flattens_a_profile():
    [p] = parse.normalise_profiles([profile("t", "nvr-main")])
    assert (p.token, p.name, p.codec, p.width, p.height, p.fps) == (
        "t",
        "nvr-main",
        "H264",
        1920,
        1080,
        15,
    )
    assert p.pixels == 1920 * 1080


def test_summary_is_the_nodes_card_line():
    [p] = parse.normalise_profiles([profile("t", "nvr-main")])
    assert p.summary == "nvr-main · 1920x1080 · 15 fps · H.264"


def test_profiles_without_video_are_dropped():
    raw = [profile("t", "nvr-main"), {"token": "audio", "Name": "audio only"}]
    assert [p.token for p in parse.normalise_profiles(raw)] == ["t"]


def test_missing_rate_control_is_tolerated():
    raw = {
        "token": "t",
        "Name": "fixed",
        "VideoEncoderConfiguration": {
            "Encoding": "H264",
            "Resolution": {"Width": 1280, "Height": 720},
        },
    }
    [p] = parse.normalise_profiles([raw])
    assert p.fps == 0 and p.pixels == 1280 * 720


def test_named_profiles_win():
    profiles = parse.normalise_profiles(AXIS_LIKE)
    main, sub = parse.choose_main_sub(profiles)
    assert main.name == "nvr-main"
    assert sub.name == "nvr-sub"


def test_falls_back_to_largest_and_smallest_h264():
    raw = [
        profile("a", "High", width=1920, height=1080),
        profile("b", "Low", width=640, height=360),
        profile("c", "Mid", width=1280, height=720),
    ]
    main, sub = parse.choose_main_sub(parse.normalise_profiles(raw))
    assert main.name == "High"
    assert sub.name == "Low"


def test_h264_is_preferred_over_a_larger_h265():
    raw = [
        profile("a", "Big H265", codec="H265", width=2688, height=1512),
        profile("b", "H264", width=1920, height=1080),
        profile("c", "Small", width=640, height=360),
    ]
    main, sub = parse.choose_main_sub(parse.normalise_profiles(raw))
    assert main.codec == "H264"
    assert sub.name == "Small"


def test_single_profile_gives_a_main_and_no_sub():
    main, sub = parse.choose_main_sub(parse.normalise_profiles([profile("a", "only")]))
    assert main.token == "a"
    assert sub is None


def test_no_profiles_gives_nothing():
    assert parse.choose_main_sub([]) == (None, None)


def test_mjpeg_only_camera_still_gets_a_main():
    raw = [profile("a", "mjpeg", codec="JPEG", width=1280, height=720)]
    main, _ = parse.choose_main_sub(parse.normalise_profiles(raw))
    assert main.token == "a"


def test_device_info_normalisation():
    device = parse.normalise_device_info(
        {
            "Manufacturer": "AXIS",
            "Model": "M1234-LVE",
            "FirmwareVersion": "10.12.338",
            "SerialNumber": "ACCC8EDBCF20",
        }
    )
    assert device.manufacturer == "AXIS"
    assert device.model == "M1234-LVE"
    assert device.as_dict()["firmware"] == "10.12.338"


def test_device_info_tolerates_an_empty_response():
    assert parse.normalise_device_info({}).model == ""


def test_discovery_address_parsing():
    cases = {
        "http://192.168.1.101/onvif/device_service": ("192.168.1.101", 80),
        "http://192.168.1.102:8080/onvif/device_service": ("192.168.1.102", 8080),
        "http://192.168.1.103/onvif/device_service http://[fe80::1]/onvif": ("192.168.1.103", 80),
        "http://[fe80::1]:8000/onvif/device_service": ("fe80::1", 8000),
        "": ("", 80),
    }
    for xaddr, expected in cases.items():
        assert parse.parse_discovery_address(xaddr) == expected


def test_slug_from_name():
    assert parse.slug_from_name("Front Door") == "front"
    assert parse.slug_from_name("Driveway") == "driveway"
    assert parse.slug_from_name("  ") == "camera"
    assert parse.slug_from_name("AXIS M1234-LVE") == "axis"


def test_slug_avoids_collisions():
    assert parse.slug_from_name("Front Door", taken={"front"}) == "front-door"
    assert parse.slug_from_name("Front Door", taken={"front", "front-door"}) == "front-2"


def test_slug_is_url_and_path_safe():
    slug = parse.slug_from_name("Back/Yard ../etc")
    assert "/" not in slug and ".." not in slug
