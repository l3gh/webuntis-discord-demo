import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord_bot as bot


class PrivateCommandTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        return SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=Mock(return_value=False)),
                               followup=SimpleNamespace(send=AsyncMock()))

    def assert_private_orange(self, call):
        self.assertTrue(call.kwargs['ephemeral'])
        self.assertEqual(call.kwargs['embed'].colour.value, 0xFF6033)

    async def test_status_is_private_and_orange(self):
        interaction = self.interaction()
        with patch.object(bot.bot, 'remember_alert_channel'), patch.object(bot.bot, 'snapshot', None), patch.object(bot.bot, 'last_checked', None), patch.object(bot.bot, 'last_error', None):
            await bot.status_command.callback(interaction)
        self.assert_private_orange(interaction.response.send_message.await_args)
        self.assertIn('No WebUntis snapshot yet', interaction.response.send_message.await_args.kwargs['embed'].description)

    async def test_check_success_and_failure_are_private_and_orange(self):
        for result in ({'initial': False, 'changes': 0}, RuntimeError('offline')):
            with self.subTest(result=result):
                interaction = self.interaction()
                poll = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
                with patch.object(bot.bot, 'remember_alert_channel'), patch.object(bot.bot, 'poll_once', poll), patch.object(bot.bot, 'last_error', None):
                    await bot.check_command.callback(interaction)
                interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
                self.assert_private_orange(interaction.followup.send.await_args)
                self.assertIn('Check failed' if isinstance(result, Exception) else 'nothing has changed', interaction.followup.send.await_args.kwargs['embed'].description)

    async def test_command_errors_stay_private_before_and_after_defer(self):
        for done in (False, True):
            interaction = self.interaction()
            interaction.command = SimpleNamespace(name='status')
            interaction.response.is_done.return_value = done
            await bot.command_error(interaction, RuntimeError('failure'))
            self.assert_private_orange((interaction.followup.send if done else interaction.response.send_message).await_args)
