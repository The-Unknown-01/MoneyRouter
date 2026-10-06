package main

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"time"

	_ "modernc.org/sqlite"
)

type database struct{ *sql.DB }

func openDatabase(path string) (*database, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	// SQLite pragmas are connection-local. A single short-lived SQL connection
	// keeps foreign key and busy-timeout behavior consistent on a tiny VPS.
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	for _, pragma := range []string{
		"PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=5000", "PRAGMA foreign_keys=ON", "PRAGMA synchronous=FULL",
	} {
		if _, err := db.Exec(pragma); err != nil {
			db.Close()
			return nil, fmt.Errorf("%s: %w", pragma, err)
		}
	}
	schema := []string{
		`CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf TEXT NOT NULL, expires_at TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, date TEXT NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('income','expense')), amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), category TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, fingerprint TEXT, created_at TEXT NOT NULL)`,
		`CREATE UNIQUE INDEX IF NOT EXISTS tx_dedupe ON transactions(user_id,fingerprint) WHERE fingerprint IS NOT NULL`,
		`CREATE INDEX IF NOT EXISTS tx_user_date ON transactions(user_id,date DESC)`,
		`CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, income_cents INTEGER NOT NULL DEFAULT 0, stable_income INTEGER NOT NULL DEFAULT 1, family_load INTEGER NOT NULL DEFAULT 0, debt_cents INTEGER NOT NULL DEFAULT 0, reserve_cents INTEGER NOT NULL DEFAULT 0, horizon_months INTEGER NOT NULL DEFAULT 36, max_loss_pct INTEGER NOT NULL DEFAULT 0, experience TEXT NOT NULL DEFAULT 'none', goal TEXT NOT NULL DEFAULT '', confirmed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, version INTEGER NOT NULL, data_json TEXT NOT NULL, narrative TEXT NOT NULL DEFAULT '', trace_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id,version))`,
		`CREATE INDEX IF NOT EXISTS plans_user ON plans(user_id,version DESC)`,
		`CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, scope TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)`,
		`CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id,id DESC)`,
	}
	for _, statement := range schema {
		if _, err := db.Exec(statement); err != nil {
			db.Close()
			return nil, err
		}
	}
	return &database{db}, nil
}

func utcNow() string { return time.Now().UTC().Format(time.RFC3339Nano) }

func (d *database) backup(path string) error {
	if _, err := os.Stat(path); err == nil {
		return errors.New("backup target already exists")
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	_, err := d.Exec(`VACUUM INTO ?`, path)
	return err
}

func (d *database) clearUserData(userID int64) error {
	tx, err := d.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, table := range []string{"messages", "plans", "transactions", "profiles"} {
		if _, err := tx.Exec("DELETE FROM "+table+" WHERE user_id=?", userID); err != nil {
			return err
		}
	}
	return tx.Commit()
}
