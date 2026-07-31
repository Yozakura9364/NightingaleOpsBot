from __future__ import annotations

import sys
from pathlib import Path
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot-plugin"
sys.path.insert(0, str(PLUGIN_ROOT))

from astrbot_plugin_short_links.commands import (  # noqa: E402
    CommandParseError,
    parse_short_link_command,
)


class ShortLinkCommandTests(unittest.TestCase):
    def test_empty_command_opens_help(self):
        self.assertEqual(parse_short_link_command("").action, "help")

    def test_plain_url_creates_random_code(self):
        command = parse_short_link_command("https://example.com/path#section")
        self.assertEqual(command.action, "create")
        self.assertEqual(command.code, "")
        self.assertEqual(command.target_url, "https://example.com/path#section")

    def test_custom_code(self):
        command = parse_short_link_command("自定义 card https://example.com")
        self.assertEqual(command.action, "create")
        self.assertEqual(command.code, "card")

    def test_custom_code_shorthand(self):
        command = parse_short_link_command("card https://example.com")
        self.assertEqual(command.action, "create")
        self.assertEqual(command.code, "card")

    def test_update_and_state_commands(self):
        self.assertEqual(parse_short_link_command("修改 card https://example.org").action, "update")
        self.assertEqual(parse_short_link_command("停用 card").action, "disable")
        self.assertEqual(parse_short_link_command("启用 card").action, "enable")
        self.assertEqual(parse_short_link_command("删除 card").action, "delete")

    def test_missing_arguments_are_rejected(self):
        with self.assertRaises(CommandParseError):
            parse_short_link_command("自定义 card")


if __name__ == "__main__":
    unittest.main()
