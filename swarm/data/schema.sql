CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS candles (
    symbol text NOT NULL,
    ts timestamptz NOT NULL,
    timeframe text NOT NULL,
    open double precision NOT NULL CHECK (open > 0),
    high double precision NOT NULL CHECK (high > 0),
    low double precision NOT NULL CHECK (low > 0),
    close double precision NOT NULL CHECK (close > 0),
    volume double precision NOT NULL CHECK (volume >= 0),
    PRIMARY KEY (symbol, timeframe, ts)
);
SELECT create_hypertable('candles', by_range('ts'), if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS book_snapshots (
    symbol text NOT NULL,
    ts timestamptz NOT NULL,
    bids jsonb NOT NULL,
    asks jsonb NOT NULL,
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('book_snapshots', by_range('ts'), if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS news (
    ts timestamptz NOT NULL,
    source text NOT NULL,
    title text NOT NULL,
    body text NOT NULL DEFAULT '',
    url text NOT NULL,
    symbol_tags text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, url)
);
CREATE INDEX IF NOT EXISTS news_ts_idx ON news (ts DESC);

CREATE TABLE IF NOT EXISTS sentiment_scores (
    symbol text NOT NULL,
    ts timestamptz NOT NULL,
    score double precision NOT NULL CHECK (score BETWEEN -1 AND 1),
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    summary text NOT NULL,
    model text NOT NULL,
    is_mock boolean NOT NULL,
    PRIMARY KEY (symbol, ts, is_mock)
);
CREATE TABLE IF NOT EXISTS sentiment_batches (
    is_mock boolean PRIMARY KEY,
    last_attempt timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS sentiment_seen (
    source text NOT NULL,
    url text NOT NULL,
    is_mock boolean NOT NULL,
    PRIMARY KEY (source, url, is_mock)
);

CREATE TABLE IF NOT EXISTS votes (
    id bigserial PRIMARY KEY,
    agent text NOT NULL,
    symbol text NOT NULL,
    ts timestamptz NOT NULL,
    signal jsonb NOT NULL,
    resulting_score double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS votes_symbol_ts_idx ON votes(symbol, ts DESC);
