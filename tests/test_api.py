import tempfile
import unittest
from pathlib import Path

from PIL import Image

from trmnl_schedule import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        image = root / "current.bmp"
        Image.new("1", (800, 480), 1).save(image)
        self.app = create_app({
            "TESTING": True,
            "DATABASE_PATH": root / "devices.sqlite3",
            "IMAGE_PATH": image,
            "PUBLIC_BASE_URL": "http://trmnl.local:5000",
            "REFRESH_RATE": 1800,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def enroll(self, path="/api/setup"):
        return self.client.get(path, headers={"ID": "AA:BB:CC:DD:EE:FF"})

    def test_setup_firmware_path_and_status(self):
        keys = []
        for path in ("/api/setup", "/api/setup/"):
            response = self.enroll(path)
            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            self.assertEqual(body["status"], 200)
            self.assertTrue(body["api_key"])
            self.assertTrue(body["friendly_id"])
            keys.append(body["api_key"])
        self.assertEqual(keys[0], keys[1])

    def test_display_uses_firmware_status_and_returns_image(self):
        setup = self.enroll().get_json()
        response = self.client.get("/api/display", headers={
            "ID": "AA:BB:CC:DD:EE:FF", "Access-Token": setup["api_key"],
        })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], 0)
        image = self.client.get(body["image_url"].replace("http://trmnl.local:5000", ""))
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.mimetype, "image/bmp")
        image.close()

    def test_display_rejects_bad_token(self):
        self.enroll()
        response = self.client.get("/api/display", headers={
            "ID": "AA:BB:CC:DD:EE:FF", "Access-Token": "wrong",
        })
        self.assertEqual(response.status_code, 401)

    def test_old_image_url_survives_a_refresh(self):
        setup = self.enroll().get_json()
        headers = {"ID": "AA:BB:CC:DD:EE:FF", "Access-Token": setup["api_key"]}
        first = self.client.get("/api/display", headers=headers).get_json()
        old_response = self.client.get(f"/display/{first['filename']}")
        old_bytes = old_response.get_data()
        old_response.close()
        image_path = Path(self.temp.name) / "current.bmp"
        Image.new("1", (800, 480), 0).save(image_path)
        second = self.client.get("/api/display", headers=headers).get_json()
        self.assertNotEqual(first["filename"], second["filename"])
        previous = self.client.get(f"/display/{first['filename']}")
        self.assertEqual(previous.status_code, 200)
        self.assertEqual(previous.get_data(), old_bytes)
        previous.close()


if __name__ == "__main__":
    unittest.main()
