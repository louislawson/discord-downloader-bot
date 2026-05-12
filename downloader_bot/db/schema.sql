CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id              BIGINT PRIMARY KEY,
    delivery_mode         TEXT NOT NULL DEFAULT 'dm'
                          CHECK (delivery_mode IN ('dm', 'channel')),
    results_channel_id    BIGINT,
    allowed_media_types   TEXT[],
    max_archive_size_bytes BIGINT,
    retention_hours       INTEGER NOT NULL DEFAULT 24,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enforce: if delivery_mode='channel', results_channel_id must be set.
-- Done as a CHECK rather than two columns so the invariant is local.
ALTER TABLE guild_settings DROP CONSTRAINT IF EXISTS guild_settings_channel_requires_id;
ALTER TABLE guild_settings ADD CONSTRAINT guild_settings_channel_requires_id
    CHECK (delivery_mode <> 'channel' OR results_channel_id IS NOT NULL);
