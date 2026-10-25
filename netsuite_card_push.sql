-- Additive ledger. Run once on the verified Iron Bot database before enabling.
CREATE TABLE IF NOT EXISTS om_ns_card_push (
  identity text PRIMARY KEY,
  board text NOT NULL,
  fingerprint text NOT NULL,
  record_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
