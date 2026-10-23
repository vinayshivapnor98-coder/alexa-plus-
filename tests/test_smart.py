"""Tests for the smart command engine (reminders, inventory expiry, music, funny replies)."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

_TEMP = tempfile.TemporaryDirectory(prefix="alexa_smart_test_")
os.environ["ALEXA_AGENT_DB"] = str(Path(_TEMP.name) / "smart.db")
os.environ["LOAD_DEMO_DATA"] = "0"
os.environ.pop("AI_API_KEY", None)
os.environ["OLLAMA_ENABLED"] = "0"

import server  # noqa: E402
import smart  # noqa: E402
import web_app  # noqa: E402

NOW = datetime(2026, 10, 23, 14, 30, 0)


class ParsingTests(unittest.TestCase):
    def due(self, text):
        return smart.parse_when(smart.clean_command(text), NOW, allow_for=True).due

    def test_relative_minutes(self):
        self.assertEqual(self.due("remind me to take medicine after 2 min"), NOW + timedelta(minutes=2))
        self.assertEqual(self.due("Alexa please remind me to take medicine in 2 minutes"), NOW + timedelta(minutes=2))
        self.assertEqual(self.due("remind me in two minutes to stretch"), NOW + timedelta(minutes=2))

    def test_compound_and_half(self):
        self.assertEqual(self.due("remind me in 1 hour 30 minutes to call mom"), NOW + timedelta(minutes=90))
        self.assertEqual(self.due("remind me in half an hour to drink water"), NOW + timedelta(minutes=30))
        self.assertEqual(self.due("remind me after 90 seconds to flip the toast"), NOW + timedelta(seconds=90))

    def test_hinglish(self):
        self.assertEqual(self.due("mujhe 2 minute baad dawai yaad dilao"), NOW + timedelta(minutes=2))

    def test_clock_times(self):
        self.assertEqual(self.due("remind me at 5 pm to submit report"), datetime(2026, 10, 23, 17, 0))
        self.assertEqual(self.due("remind me tomorrow at 8 am to go to gym"), datetime(2026, 10, 24, 8, 0))
        self.assertEqual(self.due("remind me at 9:30 pm to take tablet"), datetime(2026, 10, 23, 21, 30))
        # already past today -> tomorrow
        self.assertEqual(self.due("set an alarm for 6 am"), datetime(2026, 10, 24, 6, 0))

    def test_dates(self):
        self.assertEqual(self.due("remind me on 25 october to pay rent"), datetime(2026, 10, 25, 9, 0))
        d = smart.find_date("expires 30/10/2026", NOW.date())
        self.assertEqual(d[0], date(2026, 10, 30))
        self.assertEqual(smart.find_date("in 5 days", NOW.date())[0], date(2026, 10, 28))
        self.assertEqual(smart.find_date("day after tomorrow", NOW.date())[0], date(2026, 10, 25))

    def test_inventory_parse(self):
        p = smart.parse_inventory("Add 2 packets of bread to my inventory, expires tomorrow", NOW.date())
        self.assertEqual((p["name"], p["quantity"], p["unit"], p["expiry"]), ("Bread", 2.0, "packet", date(2026, 10, 24)))
        p = smart.parse_inventory("I bought 1 litre milk, expires on 30/10 and remind me 3 days before", NOW.date())
        self.assertEqual((p["name"], p["expiry"], p["lead_days"]), ("Milk", date(2026, 10, 30), 3))
        p = smart.parse_inventory("I bought 6 eggs, they expire on 28/10", NOW.date())
        self.assertEqual((p["name"], p["quantity"]), ("Eggs", 6.0))


class CommandTests(unittest.TestCase):
    def setUp(self):
        with server.get_db() as conn:
            conn.execute("DELETE FROM reminders")
            conn.execute("DELETE FROM inventory")
            conn.execute("DELETE FROM memories")
        smart.clear_pending()
        self._yt = smart.youtube_lookup
        smart.youtube_lookup = lambda q, timeout=8: ("dQw4w9WgXcQ", "Test Song")

    def tearDown(self):
        smart.youtube_lookup = self._yt

    def test_medicine_reminder_is_saved_with_seconds_precision(self):
        before = datetime.now()
        r = smart.handle("Alexa please remind me to take medicine after 2 min")
        self.assertTrue(r["ok"])
        self.assertIn("take medicine", r["reply"])
        rows = server.list_reminders("pending")["reminders"]
        self.assertEqual(len(rows), 1)
        due = datetime.fromisoformat(rows[0]["remind_at"])
        self.assertGreaterEqual((due - before).total_seconds(), 115)
        self.assertLessEqual((due - before).total_seconds(), 125)

    def test_reminder_asks_for_time_then_completes(self):
        r = smart.handle("remind me to call mom")
        self.assertIn("When", r["reply"])
        r = smart.handle("in 5 minutes")
        self.assertIn("call mom", r["reply"])
        self.assertEqual(server.list_reminders("pending")["count"], 1)

    def test_inventory_with_expiry_creates_reminders(self):
        r = smart.handle("add 2 packets of bread to inventory, expires in 4 days")
        self.assertTrue(r["ok"])
        items = server.list_inventory()["items"]
        self.assertEqual(items[0]["item_name"], "Bread")
        self.assertEqual(items[0]["expiry_date"], (date.today() + timedelta(days=4)).isoformat())
        reminders = server.list_reminders("pending")["reminders"]
        self.assertGreaterEqual(len(reminders), 1)
        self.assertTrue(all("Bread" in x["title"] for x in reminders))

    def test_expiry_asked_afterwards_then_updated_for_this(self):
        r = smart.handle("add milk to inventory")
        self.assertIn("expire", r["reply"].lower())
        smart.handle("in 5 days")
        self.assertEqual(server.list_inventory()["items"][0]["expiry_date"], (date.today() + timedelta(days=5)).isoformat())
        r = smart.handle("add the expiry for this is in 9 days")
        self.assertEqual(server.list_inventory()["items"][0]["expiry_date"], (date.today() + timedelta(days=9)).isoformat())

    def test_database_phrase_saves_a_note(self):
        r = smart.handle("alexa add this data in your database: my locker code hint is blue")
        self.assertIn("locker code hint is blue", r["reply"])   # reply wording varies (joke mode), the note text does not
        self.assertEqual(server.search_memories("locker")["count"], 1)

    def test_music(self):
        r = smart.handle("Alexa play Kesariya")
        self.assertEqual(r["action"]["type"], "play_youtube")
        self.assertEqual(r["action"]["video_id"], "dQw4w9WgXcQ")
        r = smart.handle("play Shape of You on spotify")
        self.assertIn("spotify", r["open_url"])
        self.assertEqual(smart.handle("stop the music")["action"]["type"], "music_stop")
        self.assertEqual(smart.handle("open youtube")["open_url"], "https://www.youtube.com/")

    def test_normal_sentences_are_not_hijacked(self):
        for t in ["start a study session tomorrow", "resume my study", "tell me a joke", "what is the capital of France", "I feel great today"]:
            self.assertIsNone(smart.handle(t), t)

    def test_due_endpoint_rings_once(self):
        client = web_app.app.test_client()
        past = (datetime.now() - timedelta(seconds=3)).isoformat(timespec="seconds")
        server.create_reminder("take medicine", past)
        d = client.get("/api/reminders/due").get_json()
        self.assertEqual(d["count"], 1)
        self.assertIn("take medicine", d["reminders"][0]["announcement"])
        self.assertEqual(client.get("/api/reminders/due").get_json()["count"], 0)

    def test_chat_endpoint_uses_smart_and_funny_fallback_speech(self):
        client = web_app.app.test_client()
        body = client.post("/api/chat", json={"message": "remind me to drink water in 3 minutes"}).get_json()
        self.assertEqual(body["mode"], "smart")
        self.assertTrue(body["ok"])
        body = client.post("/api/chat", json={"message": "tell me a story about a cat"}).get_json()
        self.assertFalse(body["ok"])           # no AI configured in tests
        self.assertIn("chai break", body["speech"])


if __name__ == "__main__":
    unittest.main()
