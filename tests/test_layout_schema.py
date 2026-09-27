from guildmaster.models.layout_schema import (
    ChannelDefinition,
    RoleDefinition,
    ServerLayout,
    sanitize_channel_name,
)


def test_channel_name_sanitized():
    ch = ChannelDefinition(name="Lore Archive!!", type="text")
    assert ch.name == "lore-archive"


def test_sanitize_channel_name_edge_cases():
    assert sanitize_channel_name("  Gen Chat  ") == "gen-chat"
    assert sanitize_channel_name("###") == "channel"
    assert sanitize_channel_name("a" * 150) == "a" * 100


def test_slowmode_clamped():
    assert ChannelDefinition(name="x", slowmode=-5).slowmode == 0
    assert ChannelDefinition(name="x", slowmode=99999).slowmode == 21600
    assert ChannelDefinition(name="x", slowmode=30).slowmode == 30


def test_defaults_public_channel():
    ch = ChannelDefinition(name="general")
    assert ch.type == "text"
    assert ch.roles_allowed == ["@everyone"]
    assert ch.roles_denied == []
    assert ch.nsfw is False
    assert ch.is_restricted is False


def test_is_restricted():
    assert ChannelDefinition(name="x", roles_allowed=["Admin"]).is_restricted
    assert ChannelDefinition(name="x", roles_denied=["Troll"]).is_restricted
    assert not ChannelDefinition(name="x", roles_allowed=["@everyone"]).is_restricted


def test_role_color_normalization():
    assert RoleDefinition(name="Admin", color="a44cd3").color == "#a44cd3"
    assert RoleDefinition(name="Admin", color="#A44CD3").color == "#A44CD3"
    assert RoleDefinition(name="Admin", color="not-a-color").color is None
    assert RoleDefinition(name="Admin").color is None


def test_server_layout_counts():
    layout = ServerLayout(
        server_summary="x",
        categories=[
            {"category_name": "A", "channels": [{"name": "one"}, {"name": "two"}]},
            {"category_name": "B", "channels": [{"name": "three", "type": "voice"}]},
        ],
    )
    assert layout.channel_count() == 3
    assert layout.categories[1].channels[0].type == "voice"


def test_full_payload_from_dict():
    payload = {
        "server_summary": "Dark fantasy RPG community",
        "roles": [{"name": "Admin", "color": "#ff0000"}],
        "categories": [
            {
                "category_name": "Admin Chambers",
                "channels": [
                    {
                        "name": "Admin Room",
                        "type": "text",
                        "topic": "staff only",
                        "roles_allowed": ["Admin"],
                        "roles_denied": ["@everyone"],
                    }
                ],
            }
        ],
    }
    layout = ServerLayout.model_validate(payload)
    ch = layout.categories[0].channels[0]
    assert ch.name == "admin-room"
    assert ch.is_restricted
