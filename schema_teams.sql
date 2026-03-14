-- Run this in your Supabase SQL editor (Dashboard → SQL Editor → New query)

-- 1. Teams table
CREATE TABLE teams (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_by  uuid REFERENCES users(id) ON DELETE SET NULL,
    invite_code text UNIQUE NOT NULL,
    created_at  timestamptz DEFAULT now()
);

-- 2. Team members table (one user, one team)
CREATE TABLE team_members (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id   uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role      text NOT NULL DEFAULT 'member',  -- 'owner' | 'member'
    joined_at timestamptz DEFAULT now(),
    UNIQUE (user_id)   -- one team per user
);

-- 3. Add team_id to contacts (nullable — solo users keep null)
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS team_id uuid REFERENCES teams(id) ON DELETE SET NULL;

-- Optional: index for team-based queries
CREATE INDEX IF NOT EXISTS contacts_team_id_idx ON contacts (team_id);
