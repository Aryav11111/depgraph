from depgraph.core.config import Settings


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=None)
    assert s.database_url.get_secret_value() == "postgresql+asyncpg://u:p@db:5432/x"
    assert s.log_level == "DEBUG"


def test_secrets_are_masked_in_repr(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:hunter2@db/x")
    assert "hunter2" not in repr(Settings(_env_file=None))
