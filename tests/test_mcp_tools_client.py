import asyncio
import threading
import unittest
from unittest.mock import Mock, patch

import mcp_tools_client


class MCPEventLoopTests(unittest.TestCase):
    def test_background_loop_runs_submitted_coroutines(self):
        original_loop = mcp_tools_client._loop
        ready = threading.Event()
        thread = threading.Thread(
            target=mcp_tools_client._run_loop_forever,
            args=(ready,),
            daemon=True,
        )
        try:
            thread.start()
            self.assertTrue(ready.wait(2))
            loop = mcp_tools_client._loop
            self.assertIsNotNone(loop)

            async def probe():
                return "running"

            future = asyncio.run_coroutine_threadsafe(probe(), loop)
            self.assertEqual(future.result(timeout=2), "running")
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(loop.is_closed())
        finally:
            loop = mcp_tools_client._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=2)
            mcp_tools_client._loop = original_loop

    @patch.object(mcp_tools_client.browse_history_store, "add_visit")
    @patch.object(mcp_tools_client.asyncio, "run_coroutine_threadsafe")
    @patch.object(mcp_tools_client, "ensure_started")
    def test_playwright_navigation_is_recorded(self, _started, submit, add_visit):
        original_loop = mcp_tools_client._loop
        self.addCleanup(setattr, mcp_tools_client, '_loop', original_loop)
        mcp_tools_client._loop = Mock()
        future = Mock()
        future.result.return_value = "Navigated to https://example.com"
        submit.return_value = future

        result = mcp_tools_client.call_tool(
            "playwright__navigate", {"url": "https://example.com"}
        )
        submit.call_args.args[0].close()

        self.assertIn("Navigated", result)
        add_visit.assert_called_once_with(
            url="https://example.com",
            title="Playwright MCP navigation",
            query="https://example.com",
            source="playwright-mcp",
            ok=True,
        )


if __name__ == "__main__":
    unittest.main()
