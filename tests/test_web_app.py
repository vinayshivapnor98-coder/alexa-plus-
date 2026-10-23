"""Integration tests using an isolated temporary database and fake data only."""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path

_TEMP = tempfile.TemporaryDirectory(prefix="alexa_agent_test_")
os.environ["ALEXA_AGENT_DB"] = str(Path(_TEMP.name) / "test_assistant.db")
os.environ["LOAD_DEMO_DATA"] = "0"
os.environ.pop("AI_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)

import server  # noqa: E402
import web_app  # noqa: E402


class AlexaWebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = web_app.app.test_client()

    def setUp(self):
        with server.get_db() as conn:
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM study_sessions")
            conn.execute("DELETE FROM inventory")
            conn.execute("DELETE FROM reminders")
            conn.execute("DELETE FROM password_entries")
            conn.execute("DELETE FROM password_vault_meta")
        server.lock_password_vault()

    def test_shopping_search_links_are_returned(self):
        response = self.client.post("/api/shopping-search", json={"query": "wireless earbuds under 2000"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["results"]), 2)
        self.assertIn("amazon.in", data["results"][0]["url"])
        self.assertIn("does not fetch", data["note"])

    def test_sync_token_round_trip(self):
        created = self.client.post("/api/sync/profile", json={}).get_json()
        token = created["token"]
        headers = {"X-Sync-Token": token}
        saved = self.client.put("/api/sync/conversation", headers=headers, json={"messages": [{"role": "user", "content": "Hello"}]})
        self.assertEqual(saved.status_code, 200)
        loaded = self.client.get("/api/sync/conversation", headers=headers).get_json()
        self.assertEqual(loaded["messages"][0]["content"], "Hello")

    def test_services_status(self):
        response = self.client.get("/api/services")
        self.assertEqual(response.status_code, 200)
        self.assertIn("shopping", response.get_json())
        self.assertIn("cross_device_sync", response.get_json())

    def test_home_and_health(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_memory_save_and_recall_chat(self):
        saved = self.client.post("/api/chat", json={"message": "Remember that I prefer vegetarian food"}).get_json()
        self.assertTrue(saved["ok"])
        recalled = self.client.post("/api/chat", json={"message": "What do you remember about me?"}).get_json()
        self.assertIn("vegetarian", recalled["reply"].lower())

    def test_study_task_add_and_list(self):
        added = self.client.post("/api/tasks", json={"title": "Revise SQL", "subject": "DBMS", "due_date": "", "estimated_minutes": 30})
        self.assertEqual(added.status_code, 200)
        tasks = self.client.get("/api/tasks?status=pending").get_json()
        self.assertEqual(tasks["count"], 1)
        self.assertEqual(tasks["tasks"][0]["title"], "Revise SQL")

    def test_inventory_and_expiry(self):
        from datetime import date, timedelta
        expiry = (date.today() + timedelta(days=1)).isoformat()
        added = self.client.post("/api/inventory", json={"item_name": "Demo Milk", "quantity": 1, "unit": "packet", "expiry_date": expiry, "category": "dairy"})
        self.assertEqual(added.status_code, 200)
        expiring = self.client.get("/api/expiring?days=7").get_json()
        self.assertEqual(expiring["count"], 1)
        self.assertEqual(expiring["items"][0]["item_name"], "Demo Milk")

    def test_dish_suggestions(self):
        response = self.client.post("/api/dishes", json={"mood": "stressed", "dietary_preference": "vegetarian", "max_cooking_minutes": 30})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["suggestions"])

    def test_password_vault_encryption_workflow(self):
        password = "FakeDemoPassphrase-2026!"
        setup = self.client.post("/api/vault/setup", json={"master_password": password}).get_json()
        self.assertTrue(setup["ok"])
        saved = self.client.post("/api/vault/entries", json={"title": "Demo account", "website": "https://example.com", "username": "demo-user", "password": "fake-password-123", "notes": "test only"}).get_json()
        self.assertTrue(saved["ok"])
        entry_id = saved["entry_id"]
        listed = self.client.get("/api/vault/entries").get_json()
        self.assertEqual(listed["count"], 1)
        self.assertNotIn("password", listed["entries"][0])
        revealed = self.client.post(f"/api/vault/entries/{entry_id}/reveal", json={}).get_json()
        self.assertEqual(revealed["password"], "fake-password-123")
        self.client.post("/api/vault/lock", json={})
        self.assertEqual(self.client.get("/api/vault/entries").status_code, 423)
        self.assertTrue(self.client.post("/api/vault/unlock", json={"master_password": password}).get_json()["ok"])


class GeneralChatTests(unittest.TestCase):
    """Regression tests: general messages must reach the AI provider instead of crashing."""

    @classmethod
    def setUpClass(cls):
        cls.client = web_app.app.test_client()

    def setUp(self):
        self._orig = web_app.optional_model_reply

    def tearDown(self):
        web_app.optional_model_reply = self._orig
        web_app.LAST_AI_ERROR["message"] = ""

    def test_general_message_reaches_ai(self):
        calls = []
        def fake(message, history, language="auto", voice=False):
            calls.append((message, voice))
            return "Pune is usually warm in October."
        web_app.optional_model_reply = fake
        r = self.client.post("/api/chat", json={"message": "What's the weather like in Pune?", "voice": True})
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["mode"], "ai")
        self.assertIn("Pune", body["reply"])
        self.assertEqual(calls, [("What's the weather like in Pune?", True)])

    def test_words_containing_eat_do_not_trigger_food_intent(self):
        web_app.optional_model_reply = lambda *a, **k: "A joke."
        for text in ["I feel great today", "please create a poem", "tell me a joke"]:
            body = self.client.post("/api/chat", json={"message": text}).get_json()
            self.assertEqual(body.get("mode"), "ai", text)

    def test_food_intent_still_works(self):
        body = self.client.post("/api/chat", json={"message": "Suggest dinner"}).get_json()
        self.assertTrue(body["ok"])
        self.assertIn("suggest", body["reply"].lower())

    def test_provider_error_is_shown_not_hidden(self):
        def failing(message, history, language="auto", voice=False):
            web_app.LAST_AI_ERROR["message"] = "Gemini HTTP 429: quota reached"
            return None
        web_app.optional_model_reply = failing
        body = self.client.post("/api/chat", json={"message": "tell me a story"}).get_json()
        self.assertFalse(body["ok"])
        self.assertIn("429", body["reply"])

    def test_ai_test_endpoint(self):
        web_app.optional_model_reply = lambda *a, **k: "Hello!"
        body = self.client.get("/api/ai-test").get_json()
        self.assertTrue(body["ok"])


class ManagementEndpointTests(unittest.TestCase):
    """Edit/delete endpoints added so every sidebar page can manage what it creates."""

    @classmethod
    def setUpClass(cls):
        cls.client = web_app.app.test_client()

    def setUp(self):
        with server.get_db() as conn:
            for t in ("inventory", "tasks", "reminders", "local_alerts", "family_profiles", "home_device_events"):
                conn.execute(f"DELETE FROM {t}")
            for t in ("visitor_messages", "family_messages"):
                conn.execute(f"DELETE FROM {t}")

    def test_inventory_update_and_delete(self):
        r = self.client.post("/api/inventory", json={"item_name": "Milk", "quantity": 2, "expiry_date": "2099-01-10"})
        item_id = r.get_json()["item_id"]
        r = self.client.patch(f"/api/inventory/{item_id}", json={"quantity": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/inventory").get_json()["items"][0]["quantity"], 5)
        self.assertEqual(self.client.patch(f"/api/inventory/{item_id}", json={"quantity": -1}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/inventory/{item_id}", json={"expiry_date": "not-a-date"}).status_code, 400)
        self.assertEqual(self.client.patch("/api/inventory/9999", json={"quantity": 1}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/inventory/{item_id}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/inventory/{item_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/inventory").get_json()["items"], [])

    def test_inventory_expiry_change_reschedules_reminders(self):
        item_id = self.client.post("/api/inventory", json={"item_name": "Curd", "expiry_date": "2099-03-01"}).get_json()["item_id"]
        before = {x["remind_at"] for x in server.list_reminders("pending")["reminders"]}
        self.client.patch(f"/api/inventory/{item_id}", json={"expiry_date": "2099-04-01"})
        after = {x["remind_at"] for x in server.list_reminders("pending")["reminders"]}
        self.assertTrue(before and after and before.isdisjoint(after))
        self.client.patch(f"/api/inventory/{item_id}", json={"expiry_date": ""})
        self.assertEqual(server.list_reminders("pending")["count"], 0)
        self.client.patch(f"/api/inventory/{item_id}", json={"expiry_date": "2099-05-01"})
        self.client.delete(f"/api/inventory/{item_id}")
        self.assertEqual(server.list_reminders("pending")["count"], 0)

    def test_task_and_reminder_delete(self):
        tid = self.client.post("/api/tasks", json={"title": "Revise SQL"}).get_json()["task_id"]
        self.assertEqual(self.client.delete(f"/api/tasks/{tid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/tasks/{tid}").status_code, 404)
        rid = self.client.post("/api/reminders", json={"title": "Medicine", "remind_at": "2099-01-01T09:00"}).get_json()["reminder_id"]
        self.assertEqual(self.client.delete(f"/api/reminders/{rid}").status_code, 200)
        self.assertEqual(self.client.get("/api/reminders").get_json()["count"], 0)

    def test_alert_and_family_profile_delete(self):
        aid = self.client.post("/api/local-alerts", json={"title": "Rain", "severity": "WARNING"}).get_json()["id"]
        self.assertEqual(self.client.delete(f"/api/local-alerts/{aid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/local-alerts/{aid}").status_code, 404)
        self.client.post("/api/family-profiles", json={"action": "add", "name": "Mom"})
        pid = self.client.post("/api/family-profiles", json={"action": "list"}).get_json()["profiles"][0]["id"]
        self.assertEqual(self.client.post("/api/family-profiles", json={"action": "delete", "id": pid}).status_code, 200)
        self.assertEqual(self.client.post("/api/family-profiles", json={"action": "delete", "id": pid}).status_code, 404)

    def test_visitor_message_delete(self):
        mid = self.client.post("/api/visitor/messages", json={"message": "Parcel at gate"}).get_json()["id"]
        self.assertEqual(self.client.delete(f"/api/visitor/messages/{mid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/visitor/messages/{mid}").status_code, 404)

    def test_family_voice_note_delete_removes_audio_file(self):
        import io
        data = {"sender": "Dad", "recipient": "Everyone", "audio": (io.BytesIO(b"fake-audio"), "note.webm")}
        r = self.client.post("/api/family-messages", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201)
        mid = r.get_json()["id"]
        with server.get_db() as conn:
            fname = conn.execute("SELECT audio_filename FROM family_messages WHERE id=?", (mid,)).fetchone()[0]
        self.assertTrue((web_app.FAMILY_AUDIO_DIR / fname).exists())
        self.assertEqual(self.client.delete(f"/api/family-messages/{mid}").status_code, 200)
        self.assertFalse((web_app.FAMILY_AUDIO_DIR / fname).exists())
        self.assertEqual(self.client.delete(f"/api/family-messages/{mid}").status_code, 404)

    def test_safety_warns_when_appliance_switched_on_in_the_dark(self):
        def toggle(device, state):
            return self.client.post("/api/home-devices", json={"device": device, "state": state}).get_json()
        self.assertTrue(toggle("Heater", "ON")["warnings"])            # bathroom light never turned on
        self.assertFalse(toggle("Bathroom Light", "ON")["warnings"])   # light on -> fine
        self.assertTrue(toggle("Bathroom Light", "OFF")["warnings"])   # light off again while heater ON
        self.assertFalse(toggle("Heater", "OFF")["warnings"])
        self.assertTrue(toggle("Gas Stove", "ON")["warnings"])
        self.assertFalse(toggle("Kitchen Light", "ON")["warnings"])


if __name__ == "__main__":
    unittest.main()
