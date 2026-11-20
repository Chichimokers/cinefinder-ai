import asyncio

from app.ai.tools.base import ToolContext, registry
from app.ai.tools.registry_setup import register_all


def test_all_tools_registered():
    register_all()
    names = set(registry.names())
    expected = {
        "analyze_image", "analyze_multiple_images", "extract_text_from_image", "identify_movie_candidates",
        "search_tmdb", "search_omdb", "search_wikidata", "search_movie_images", "search_web",
        "search_youtube", "search_internet_archive", "search_telegram_authorized", "search_catalog",
        "check_duplicate_movie", "save_movie", "update_movie", "get_movie_details", "get_movie_sources",
        "verify_movie_identity", "compare_movie_candidates", "calculate_confidence", "verify_source",
        "find_authorized_media", "check_distribution_rights", "download_authorized_media",
        "verify_file_integrity", "send_authorized_file", "send_authorized_link",
    }
    assert expected.issubset(names), expected - names


def test_admin_tools_hidden_for_normal_users():
    schemas = registry.schemas(is_owner=False)
    names = {s["function"]["name"] for s in schemas}
    assert "update_movie" not in names
    assert "search_telegram_authorized" not in names
    assert "analyze_image" in names


def test_unknown_tool():
    async def run():
        return await registry.execute("tool_inexistente", {}, ToolContext(db=None))

    result = asyncio.run(run())
    assert result.ok is False
    assert "desconocida" in result.error


def test_validation_error():
    async def run():
        return await registry.execute("search_tmdb", {}, ToolContext(db=None))

    result = asyncio.run(run())
    assert result.ok is False
    assert "query" in result.error
