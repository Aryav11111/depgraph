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


def test_empty_redis_url_disables_redis(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    assert Settings(_env_file=None).redis_url is None


def test_database_password_overrides_the_url_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@10.0.0.2:5432/x")
    monkeypatch.setenv("DATABASE_PASSWORD", "p@ss/word")
    s = Settings(_env_file=None)
    assert s.sqlalchemy_url() == "postgresql+asyncpg://u:p%40ss%2Fword@10.0.0.2:5432/x"
    assert "p@ss" not in repr(s)


def test_without_database_password_the_url_is_used_as_is(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    assert Settings(_env_file=None).sqlalchemy_url() == "postgresql+asyncpg://u:p@db:5432/x"
