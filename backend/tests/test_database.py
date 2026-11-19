from app.database.session import create_database


def test_database_creation(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "database_path", tmp_path / "database" / "aiclipping.db")
    engine = create_database()

    assert engine is not None
    assert (tmp_path / "database" / "aiclipping.db").exists()
