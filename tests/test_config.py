import pytest

from nightwire import config


def _env(**overrides):
    base = {
        "NVR_PASSWORD": "correct-horse",
        "NVR_SECRET_KEY": "s" * 40,
        "NVR_PORT": "8080",
        "TZ": "America/New_York",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_loads_a_complete_environment():
    settings = config.load(_env())
    assert settings.password == "correct-horse"
    assert settings.port == 8080
    assert settings.timezone == "America/New_York"
    assert settings.db_path.name == "nightwire.db"


@pytest.mark.parametrize("missing", ["NVR_PASSWORD", "NVR_SECRET_KEY"])
def test_refuses_to_start_without_a_secret(missing):
    with pytest.raises(config.ConfigError, match=missing):
        config.load(_env(**{missing: None}))


@pytest.mark.parametrize("example", ["change-me", "CHANGE-ME", "password", "  ", "todo"])
def test_refuses_example_values(example):
    with pytest.raises(config.ConfigError, match="example value"):
        config.load(_env(NVR_SECRET_KEY=example))


def test_refuses_a_short_secret_key():
    with pytest.raises(config.ConfigError, match="at least 32"):
        config.load(_env(NVR_SECRET_KEY="too-short"))


def test_refuses_a_short_password():
    with pytest.raises(config.ConfigError, match="at least 8"):
        config.load(_env(NVR_PASSWORD="abc"))


@pytest.mark.parametrize("bad", ["not-a-number", "0", "70000"])
def test_rejects_a_bad_port(bad):
    with pytest.raises(config.ConfigError):
        config.load(_env(NVR_PORT=bad))


def test_port_defaults_to_8080():
    assert config.load(_env(NVR_PORT=None)).port == 8080
