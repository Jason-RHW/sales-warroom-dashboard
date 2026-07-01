-- Sales Command Center — Supabase migration
-- Run this once in: Supabase Dashboard → SQL Editor → New Query → Run
-- Safe to run multiple times (uses IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS call_events (
    id                  SERIAL PRIMARY KEY,

    -- Who / what
    sdr_name            VARCHAR     NOT NULL,
    company_name        VARCHAR     NOT NULL DEFAULT 'Unknown Company',
    state               VARCHAR(2)  NOT NULL DEFAULT 'CA',
    industry            VARCHAR,

    -- Source linkage
    aircall_call_id     VARCHAR     UNIQUE,       -- NULL = mock/seed data
    hubspot_company_id  VARCHAR,

    -- Call lifecycle
    status              VARCHAR     NOT NULL,     -- dialing | ringing | connected | ended | voicemail
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    outcome             VARCHAR,                  -- answered | voicemail | missed | NULL (in progress)

    -- Tags (comma-separated, e.g. "Spoke with Contact,Send Sample")
    tags                VARCHAR,

    -- Timestamps (stored as naive UTC)
    started_at          TIMESTAMP   NOT NULL,
    ended_at            TIMESTAMP,
    talk_seconds        FLOAT,
    created_at          TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- Indexes used by the aggregator's daily-window query and active-call lookups
CREATE INDEX IF NOT EXISTS idx_ce_started_at       ON call_events (started_at);
CREATE INDEX IF NOT EXISTS idx_ce_is_active        ON call_events (is_active);
CREATE INDEX IF NOT EXISTS idx_ce_aircall_call_id  ON call_events (aircall_call_id);
CREATE INDEX IF NOT EXISTS idx_ce_sdr_name         ON call_events (sdr_name);
CREATE INDEX IF NOT EXISTS idx_ce_state            ON call_events (state);
CREATE INDEX IF NOT EXISTS idx_ce_outcome          ON call_events (outcome);
