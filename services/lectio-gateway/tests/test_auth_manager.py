import asyncio

from lectio_gateway.auth.manager import AuthManager


class FakeBrowser:
    def __init__(self):
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1


def test_gateway_startup_removes_a_browser_left_by_a_previous_process(tmp_path):
    manager = AuthManager(
        data_dir=tmp_path,
        browser_url="http://browser:8765",
        browser_view_url="http://localhost:6080/vnc.html",
        login_url="https://www.lectio.dk/",
    )
    browser = FakeBrowser()
    manager._browser = browser

    asyncio.run(manager.initialize())

    assert browser.stop_calls == 1
