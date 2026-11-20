from app.database.base import Base
from app.catalog.service import normalize_title
from app.services.research import parse_agent_answer


def test_all_tables_registered():
    expected = {
        "movies", "movie_aliases", "movie_images", "movie_sources", "movie_candidates",
        "search_history", "search_jobs", "agent_tasks", "agent_usage", "telegram_channels",
        "telegram_messages", "telegram_catalog_publications", "authorized_sources",
        "media_assets", "delivery_jobs", "app_users", "system_settings",
    }
    assert expected.issubset(set(Base.metadata.tables))
    assert len(expected) == 17


def test_normalize_title():
    assert normalize_title("El Viaje de Chihiro (2001)") == "el viaje de chihiro 2001"


def test_parse_agent_answer():
    result = parse_agent_answer('```json\n{"movie": {"title": "X"}, "confidence": 0.8}\n```')
    assert result["confidence"] == 0.8
    assert parse_agent_answer("texto sin json")["need_review"] is True
