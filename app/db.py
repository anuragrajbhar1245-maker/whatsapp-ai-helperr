"""SQLite storage: dedupe, chat memory, bookings, paused chats."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages (phone, id);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    business TEXT NOT NULL,
    name TEXT NOT NULL,
    date TEXT NOT NULL,
    guests TEXT,
    service TEXT,
    notes TEXT,
    raw_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paused_chats (
    phone TEXT PRIMARY KEY,
    reason TEXT,
    paused_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- dedupe ----------
    def mark_processed(self, message_id: str) -> bool:
        """Return True if this message id is new, False if we already saw it."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, created_at) VALUES (?, ?)",
                (message_id, time.time()),
            )
            # keep the table small: drop ids older than 7 days
            self._conn.execute(
                "DELETE FROM processed_messages WHERE created_at < ?", (time.time() - 7 * 86400,)
            )
            self._conn.commit()
            return cur.rowcount == 1

    # ---------- memory ----------
    def add_message(self, phone: str, role: str, content: str, limit: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (phone, role, content, created_at) VALUES (?, ?, ?, ?)",
                (phone, role, content, time.time()),
            )
            self._conn.execute(
                """DELETE FROM messages WHERE phone = ? AND id NOT IN (
                       SELECT id FROM messages WHERE phone = ? ORDER BY id DESC LIMIT ?)""",
                (phone, phone, limit),
            )
            self._conn.commit()

    def get_history(self, phone: str, limit: int) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE phone = ? ORDER BY id DESC LIMIT ?",
                (phone, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_history(self, phone: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE phone = ?", (phone,))
            self._conn.commit()

    # ---------- bookings ----------
    def save_booking(self, phone: str, business: str, booking: dict[str, Any]) -> tuple[int, bool]:
        """Save a booking. Returns (booking_id, created). Exact repeats are not saved twice."""
        with self._lock:
            existing = self._conn.execute(
                """SELECT id FROM bookings WHERE phone = ? AND business = ? AND name = ? AND date = ?
                   AND IFNULL(guests, '') = ? AND IFNULL(service, '') = ?""",
                (
                    phone,
                    business,
                    booking["name"],
                    booking["date"],
                    booking.get("guests") or "",
                    booking.get("service") or "",
                ),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cur = self._conn.execute(
                """INSERT INTO bookings (phone, business, name, date, guests, service, notes, raw_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    phone,
                    business,
                    booking["name"],
                    booking["date"],
                    booking.get("guests"),
                    booking.get("service"),
                    booking.get("notes"),
                    json.dumps(booking, ensure_ascii=False),
                    time.time(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid), True

    def list_bookings(self, phone: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if phone:
                rows = self._conn.execute(
                    "SELECT * FROM bookings WHERE phone = ? ORDER BY id", (phone,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM bookings ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---------- human handoff ----------
    def pause_chat(self, phone: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO paused_chats (phone, reason, paused_at) VALUES (?, ?, ?)",
                (phone, reason, time.time()),
            )
            self._conn.commit()

    def resume_chat(self, phone: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM paused_chats WHERE phone = ?", (phone,))
            self._conn.commit()
            return cur.rowcount > 0

    def is_paused(self, phone: str, pause_hours: int = 0) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT paused_at FROM paused_chats WHERE phone = ?", (phone,)
            ).fetchone()
        if not row:
            return False
        if pause_hours > 0 and time.time() - row["paused_at"] > pause_hours * 3600:
            self.resume_chat(phone)  # auto-resume so a forgotten chat does not stay dead
            return False
        return True

    def list_paused(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT phone FROM paused_chats ORDER BY paused_at").fetchall()
        return [r["phone"] for r in rows]
