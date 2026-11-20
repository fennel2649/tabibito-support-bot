from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tabibito.bot import TabibitoBot
from tabibito.core import Config, Store


class RoleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = Config(
            1, Path(":memory:"), False, True, 2, 3, frozenset({4}),
            frozenset(), frozenset(), frozenset(), frozenset(),
        )
        self.store = Store(config.database, config.guild_id)
        self.addCleanup(self.store.db.close)
        self.bot = TabibitoBot(config, self.store)
        self.roles = set()
        self.edits = []
        self.guild = SimpleNamespace(id=1, fetch_member=AsyncMock(side_effect=self.member))
        self.bot.edit_role = AsyncMock(side_effect=self.edit)

    def member(self, user):
        roles = self.roles.copy()
        return SimpleNamespace(
            id=user, bot=False, guild=self.guild, joined_at=None,
            get_role=lambda role: role if role in roles else None,
        )

    async def edit(self, member, role, *, add, actor):
        self.edits.append((role, add, actor))
        if add:
            self.roles.add(role)
        else:
            self.roles.remove(role)

    async def test_stale_cache_never_adds_support_to_existing_graduate(self):
        cached = self.member(10)
        self.roles.add(3)
        await self.bot.reconcile(cached)
        self.bot.edit_role.assert_not_awaited()
        self.assertEqual(self.roles, {3})

    async def test_graduation_resumes_after_removal_failure(self):
        self.roles.add(2)
        self.store.request(10, 20)

        async def fail_removal(member, role, *, add, actor):
            if not add:
                raise RuntimeError("Discord unavailable")
            await self.edit(member, role, add=add, actor=actor)

        self.bot.edit_role.side_effect = fail_removal
        with self.assertRaises(RuntimeError):
            await self.bot.reconcile(self.member(10))
        self.assertEqual(self.roles, {2, 3})
        self.assertEqual(self.store.requester(10), 20)
        self.bot.edit_role.side_effect = self.edit
        await self.bot.reconcile(self.member(10))
        self.assertEqual(self.roles, {3})
        self.assertIsNone(self.store.requester(10))
        self.assertEqual(self.edits, [(3, True, 20), (2, False, 20)])

    async def test_manual_graduation_during_support_add_is_reconciled(self):
        async def concurrent_edit(member, role, *, add, actor):
            if add:
                self.roles.add(3)
            await self.edit(member, role, add=add, actor=actor)

        self.bot.edit_role.side_effect = concurrent_edit
        await self.bot.reconcile(self.member(10))
        self.assertEqual(self.roles, {3})
        self.assertEqual(self.edits, [(2, True, None), (2, False, None)])

    async def test_preview_does_not_fetch_or_change_roles(self):
        self.bot.config = replace(self.bot.config, dry_run=True)
        self.assertEqual(await self.bot.reconcile(self.member(10), actor=10), "graduate")
        self.guild.fetch_member.assert_not_called()
        self.bot.edit_role.assert_not_called()
        self.assertIsNone(self.store.requester(10))

    async def test_registers_commands_for_target_guild(self):
        commands = {command.name for command in self.bot.tree.get_commands(guild=self.bot.target)}
        self.assertEqual(commands, {"交流支援終了", "メンバーの交流支援終了", "活動記録", "記録修正", "ロール履歴"})
        self.assertFalse(self.bot.tree.get_commands())
