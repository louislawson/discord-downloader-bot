CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id           BIGINT PRIMARY KEY,
    delivery_mode      TEXT NOT NULL
                       CHECK (delivery_mode IN ('dm', 'channel', 'both'))
                       DEFAULT 'dm',
    results_channel_id BIGINT,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
