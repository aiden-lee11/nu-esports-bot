DROP TABLE IF EXISTS prediction_bets;
DROP TABLE IF EXISTS predictions;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS reservations;

CREATE TABLE users
(
    discordid BIGINT PRIMARY KEY,
    points BIGINT DEFAULT 0
);

CREATE TABLE reservations
(
    id SERIAL PRIMARY KEY,
    team VARCHAR(50) NOT NULL,
    pcs INTEGER[] NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    manager VARCHAR(100) NOT NULL,
    is_prime_time BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE predictions
(
    id SERIAL PRIMARY KEY,
    creator_id BIGINT NOT NULL,
    title VARCHAR(255) NOT NULL,
    option_a VARCHAR(100) NOT NULL,
    option_b VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',  -- active, locked, completed, refunded
    winner VARCHAR(100),
    thread_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE prediction_bets
(
    id SERIAL PRIMARY KEY,
    prediction_id INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    option VARCHAR(100) NOT NULL,
    points INTEGER NOT NULL,
    UNIQUE(prediction_id, user_id)
);
