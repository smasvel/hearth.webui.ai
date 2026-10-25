import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import mcp_tools_client


class MCPEventLoopTests(unittest.TestCase):
    def test_chat_tool_subset_keeps_browser_workflow_and_drops_advanced_tools(self):
        names = ["browser_navigate", "browser_snapshot", "browser_find", "browser_wait_for",
                 "browser_click", "browser_type", "browser_fill_form", "browser_tabs",
                 "browser_run_code_unsafe", "browser_take_screenshot", "execute_python"]
        tools = [{"type": "function", "function": {"name": "playwright__" + name}}
                 for name in names]
        selected = mcp_tools_client.select_chat_tools(tools)
        selected_names = [tool["function"]["name"] for tool in selected]
        self.assertIn("playwright__browser_navigate", selected_names)
        self.assertIn("playwright__browser_snapshot", selected_names)
        self.assertIn("playwright__browser_find", selected_names)
        self.assertIn("playwright__browser_click", selected_names)
        self.assertIn("playwright__execute_python", selected_names)
        self.assertNotIn("playwright__browser_run_code_unsafe", selected_names)
        self.assertNotIn("playwright__browser_take_screenshot", selected_names)

    def test_large_browser_result_is_bounded_with_beginning_and_end_preserved(self):
        result = "START" + ("x" * 5000) + "END"
        compacted = mcp_tools_client.compact_chat_tool_result(result)
        self.assertLessEqual(len(compacted), mcp_tools_client.CHAT_TOOL_RESULT_LIMIT)
        self.assertTrue(compacted.startswith("START"))
        self.assertTrue(compacted.endswith("END"))
        self.assertIn("omitted to fit model context", compacted)


class PlaywrightNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_inspects_every_tab_including_popup(self):
        with patch.object(mcp_tools_client, "_call", new_callable=AsyncMock) as call:
            call.side_effect = [
                "Navigated to https://example.com", "Waited 2 seconds",
                "- 0: (current) [Example](https://example.com)\n- 1: [Popup](https://example.com/popup)",
                "Selected tab 0", "Waited 2 seconds", "- heading \\\"Example Domain\\\"",
                "Selected tab 1", "Waited 2 seconds", "- heading \\\"Popup content\\\"",
            ]
            result = await mcp_tools_client._browser_action_and_inspect(
                "browser_navigate", {"url": "https://example.com"})
        self.assertIn("Page-load confirmation", result)
        self.assertIn("Example Domain", result)
        self.assertIn("Popup", result)
        self.assertIn("Popup content", result)
        self.assertEqual(call.await_args_list[0].args,
                         ("playwright", "browser_navigate", {"url": "https://example.com"}))
        self.assertEqual(call.await_args_list[1].args,
                         ("playwright", "browser_wait_for", {"time": 2}))
        self.assertEqual(call.await_args_list[2].args,
                         ("playwright", "browser_tabs", {"action": "list"}))
        self.assertEqual(call.await_args_list[3].args,
                         ("playwright", "browser_tabs", {"action": "select", "index": 0}))
        self.assertEqual(call.await_args_list[6].args,
                         ("playwright", "browser_tabs", {"action": "select", "index": 1}))

    async def test_navigation_error_does_not_claim_page_load_confirmation(self):
        with patch.object(mcp_tools_client, "_call", new_callable=AsyncMock,
                          return_value="Tool error: navigation timed out") as call:
            result = await mcp_tools_client._browser_action_and_inspect(
                "browser_navigate", {"url": "https://example.com"})
        self.assertIn("navigation timed out", result)
        self.assertNotIn("Page-load confirmation", result)
        call.assert_awaited_once()

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
