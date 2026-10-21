CREATE TABLE IF NOT EXISTS vpn_bot.devices (
    id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    email TEXT NOT NULL,
    client_ref TEXT NOT NULL,
    platform TEXT NOT NULL,
    app_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS devices_owner ON vpn_bot.devices(user_id, email, client_ref);
ALTER TABLE vpn_bot.devices ENABLE ROW LEVEL SECURITY;
CREATE TABLE IF NOT EXISTS vpn_bot.device_inputs (
    user_id BIGINT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    prompt_id BIGINT NOT NULL,
    anchor_id BIGINT NOT NULL,
    payload TEXT NOT NULL,
    expires DOUBLE PRECISION NOT NULL
);
ALTER TABLE vpn_bot.device_inputs ENABLE ROW LEVEL SECURITY;
