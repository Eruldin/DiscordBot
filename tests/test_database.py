async def test_settings_roundtrip(db):
    assert await db.get_setting(1, "missing") is None
    assert await db.get_setting(1, "missing", 42) == 42
    await db.set_setting(1, "log_channel_id", 123456)
    assert await db.get_setting(1, "log_channel_id") == 123456
    await db.set_setting(1, "log_channel_id", None)
    assert await db.get_setting(1, "log_channel_id") is None
    # guilds are isolated
    await db.set_setting(2, "log_channel_id", 999)
    assert await db.get_setting(1, "log_channel_id") is None
    assert await db.get_setting(2, "log_channel_id") == 999


async def test_strikes(db):
    assert await db.count_strikes(1, 100) == 0
    assert await db.add_strike(1, 100, 200, "spam") == 1
    assert await db.add_strike(1, 100, 200, "spam again") == 2
    assert await db.add_strike(1, 999, 200, "other user") == 1

    strikes = await db.list_strikes(1, 100)
    assert len(strikes) == 2
    assert strikes[0]["reason"] == "spam again"  # newest first
    assert strikes[0]["moderator_id"] == 200

    removed = await db.clear_strikes(1, 100)
    assert removed == 2
    assert await db.count_strikes(1, 100) == 0


async def test_layout_backups(db):
    assert await db.latest_backup(1) is None
    first = await db.save_layout_backup(1, {"categories": [{"name": "A"}]})
    second = await db.save_layout_backup(1, {"categories": [{"name": "B"}]})
    assert second > first

    latest = await db.latest_backup(1)
    assert latest["id"] == second
    assert latest["snapshot"]["categories"][0]["name"] == "B"

    backups = await db.list_backups(1)
    assert [b["id"] for b in backups] == [second, first]


async def test_ordering_with_identical_timestamps(db):
    """Windows' ~15.6ms time.time() granularity ties back-to-back inserts;
    newest-first must hold even when created_at collides."""
    fixed_ts = 1700000000.0
    for reason in ("older", "newer"):
        await db.conn.execute(
            "INSERT INTO strikes (guild_id, user_id, moderator_id, reason, created_at) "
            "VALUES (1, 100, 200, ?, ?)",
            (reason, fixed_ts),
        )
    await db.conn.commit()
    strikes = await db.list_strikes(1, 100)
    assert [s["reason"] for s in strikes] == ["newer", "older"]

    for name in ("older", "newer"):
        await db.conn.execute(
            "INSERT INTO layout_backups (guild_id, snapshot, created_at) "
            "VALUES (1, ?, ?)",
            (f'{{"name": "{name}"}}', fixed_ts),
        )
    await db.conn.commit()
    latest = await db.latest_backup(1)
    assert latest["snapshot"]["name"] == "newer"
    backups = await db.list_backups(1)
    assert [b["id"] for b in backups[:2]] == [2, 1]  # higher id = newer row
