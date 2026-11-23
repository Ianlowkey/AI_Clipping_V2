from app.database.session import create_database
from sqlalchemy import inspect, text


def test_database_creation(tmp_path, monkeypatch):
    from app.core import config

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config.settings, "data_dir", data_dir)
    monkeypatch.setattr(config.settings, "database_path", data_dir / "database" / "aiclipping.db")
    engine = create_database()

    assert engine is not None
    assert (data_dir / "database" / "aiclipping.db").exists()
    assert "selected_for_render" in {column["name"] for column in inspect(engine).get_columns("highlight_candidates")}
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE highlight_candidates DROP COLUMN selected_for_render"))

    upgraded = create_database()
    assert "selected_for_render" in {column["name"] for column in inspect(upgraded).get_columns("highlight_candidates")}
