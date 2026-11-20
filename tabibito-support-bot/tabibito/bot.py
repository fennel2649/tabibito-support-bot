import asyncio
import logging
import time

import discord
from discord import app_commands

from .core import HOUR, VoiceTracker, included, role_action

log = logging.getLogger(__name__)


class TabibitoBot(discord.Client):
    def __init__(self, config, store):
        intents = discord.Intents.none()
        intents.guilds = intents.members = intents.voice_states = intents.guild_messages = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.store = store
        self.voice = VoiceTracker(store)
        self.voice_online = False
        self.tree = app_commands.CommandTree(self)
        self.target = discord.Object(id=config.guild_id)
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.queue = asyncio.Queue()
        self.queued = set()
        self.retry_at = {}
        self.preview = {}
        self.jobs = []
        self.register_commands()

    async def setup_hook(self):
        await self.tree.sync(guild=self.target)
        self.jobs = [
            asyncio.create_task(self.maintain()),
            asyncio.create_task(self.process_roles()),
        ]

    async def close(self):
        self.voice_online = False
        self.voice.active.clear()
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        await super().close()

    def lock(self, user):
        return self.locks[user % len(self.locks)]

    def voice_included(self, channel):
        return channel is not None and included(
            {channel.id}, self.config.voice_include,
            self.config.voice_exclude | {getattr(channel.guild.afk_channel, "id", None)},
        )

    def restore_voice(self, guild):
        occupants = {
            user
            for channel in [*guild.voice_channels, *guild.stage_channels]
            if self.voice_included(channel)
            for user in channel.voice_states
            if (member := guild.get_member(user)) and not member.bot
        }
        self.voice.reset(occupants, time.monotonic())
        self.voice_online = True

    async def on_ready(self):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            log.error("設定したサーバーに Bot が参加していません")
            return
        if not guild.unavailable:
            if not self.voice_online:
                self.restore_voice(guild)
            self.scan(guild)
        log.info("接続: %s / dry_run=%s", guild.name, self.config.dry_run)

    async def on_resumed(self):
        guild = self.get_guild(self.config.guild_id)
        if guild and not guild.unavailable:
            self.restore_voice(guild)
            self.scan(guild)

    async def on_disconnect(self):
        self.voice_online = False
        self.voice.active.clear()

    async def on_guild_unavailable(self, guild):
        if guild.id == self.config.guild_id:
            await self.on_disconnect()

    async def on_guild_available(self, guild):
        if guild.id == self.config.guild_id and self.is_ready():
            self.restore_voice(guild)
            self.scan(guild)

    def enqueue(self, user):
        if user not in self.queued and time.monotonic() >= self.retry_at.get(user, 0):
            self.queued.add(user)
            self.queue.put_nowait(user)

    def target_member(self, member):
        return member.guild.id == self.config.guild_id and not member.bot

    def held(self, member):
        return member.get_role(self.config.graduate) is not None, member.get_role(self.config.support) is not None

    def plan(self, member, requested=False):
        graduate, support = self.held(member)
        eligible = self.config.auto_enroll_existing or (
            member.joined_at is not None and member.joined_at.timestamp() >= self.store.installed_at
        )
        new = self.store.arrived(member.id)
        return role_action(
            self.store.activity(member.id), graduate=graduate, support=support,
            eligible=eligible or new, new=new, requested=requested,
        )

    def consider(self, member):
        pending = self.store.requester(member.id) is not None
        action = self.plan(member, pending)
        if action is None:
            self.preview.pop(member.id, None)
        if action or self.store.arrived(member.id) or pending:
            self.enqueue(member.id)

    def scan(self, guild):
        for member in guild.members:
            if not member.bot:
                self.consider(member)

    async def on_member_join(self, member):
        if self.target_member(member):
            self.store.arrival(member.id)
            self.enqueue(member.id)

    async def on_member_remove(self, member):
        if self.target_member(member):
            self.store.clear_arrival(member.id)
            self.preview.pop(member.id, None)
            self.retry_at.pop(member.id, None)
            if self.voice_online:
                self.voice.change(member.id, True, False, time.monotonic(), time.time())

    async def on_member_update(self, before, after):
        if not self.target_member(after):
            return
        if self.held(before) != self.held(after):
            for label, old, new in zip(("いっぱし", "交流支援"), self.held(before), self.held(after)):
                if old != new:
                    self.store.audit(after.id, "Discordで観測", f"{label} {'付与' if new else '解除'}")
            self.consider(after)

    async def on_message(self, message):
        if (
            message.guild is None or message.guild.id != self.config.guild_id
            or message.author.bot or message.webhook_id is not None
            or message.type not in (discord.MessageType.default, discord.MessageType.reply)
        ):
            return
        channel_ids = {message.channel.id}
        if isinstance(message.channel, discord.Thread):
            channel_ids.add(message.channel.parent_id)
        if included(channel_ids, self.config.text_include, self.config.text_exclude):
            self.store.post(message.author.id, message.id)

    async def on_voice_state_update(self, member, before, after):
        if self.voice_online and self.target_member(member) and before.channel != after.channel:
            self.voice.change(
                member.id, self.voice_included(before.channel), self.voice_included(after.channel),
                time.monotonic(), time.time(),
            )
            self.consider(member)

    async def maintain(self):
        await self.wait_until_ready()
        while not self.is_closed():
            guild = self.get_guild(self.config.guild_id)
            if self.voice_online and guild and not guild.unavailable and self.is_ready():
                self.voice.tick(time.monotonic())
                self.scan(guild)
            await asyncio.sleep(15)

    async def process_roles(self):
        await self.wait_until_ready()
        while not self.is_closed():
            user = await self.queue.get()
            try:
                guild = self.get_guild(self.config.guild_id)
                member = guild.get_member(user) if guild and not guild.unavailable else None
                if member and self.is_ready() and self.voice_online:
                    async with self.lock(user):
                        if time.monotonic() >= self.retry_at.get(user, 0):
                            await self.reconcile(member)
                            self.retry_at.pop(user, None)
            except (discord.HTTPException, RuntimeError) as error:
                self.retry_at[user] = time.monotonic() + 60
                log.warning("ロール処理を保留: user=%s %s", user, error)
            finally:
                self.queued.discard(user)
                self.queue.task_done()
            if not self.config.dry_run:
                await asyncio.sleep(1)

    async def reconcile(self, cached, *, actor=None):
        user = cached.id
        pending = self.store.requester(user)
        requested = actor is not None or pending is not None
        action = self.plan(cached, requested)
        if self.config.dry_run:
            if action and self.preview.get(user) != action:
                self.preview[user] = action
                log.info("dry_run: user=%s action=%s", user, action)
            return action

        if not (action or requested or self.store.arrived(user)):
            return None
        guild = cached.guild
        # Discord のキャッシュにロール変更が届く前でも、最新の API 応答を判定に使う。
        member = await guild.fetch_member(user)
        action = self.plan(member, requested)
        audit_actor = pending if pending is not None else actor
        if action == "graduate":
            await self.edit_role(member, self.config.graduate, add=True, actor=audit_actor)
            member = await guild.fetch_member(user)
            graduate, support = self.held(member)
            if not graduate:
                raise RuntimeError("いっぱしの付与を確認できませんでした")
            if support:
                await self.edit_role(member, self.config.support, add=False, actor=audit_actor)
        elif action == "remove_support":
            await self.edit_role(member, self.config.support, add=False, actor=audit_actor)
        elif action == "add_support":
            await self.edit_role(member, self.config.support, add=True, actor=None)
            # 管理人が同時に手動でいっぱしを付けた場合は両ロールの状態を解消する。
            member = await guild.fetch_member(user)
            if all(self.held(member)):
                await self.edit_role(member, self.config.support, add=False, actor=None)
        if pending is not None:
            self.store.complete(user)
        self.store.clear_arrival(user)
        return action

    async def edit_role(self, member, role_id, *, add, actor):
        role = member.guild.get_role(role_id)
        bot_member = member.guild.me
        if (
            role is None or role.managed or bot_member is None
            or not bot_member.guild_permissions.manage_roles or bot_member.top_role <= role
        ):
            raise RuntimeError(f"ロール ID {role_id} を操作できません。権限とロール順序を確認してください")
        reason = "旅人鯖交流支援Bot" + (f" / requested by {actor}" if actor is not None else "")
        if add:
            await member.add_roles(role, reason=reason, atomic=True)
        else:
            await member.remove_roles(role, reason=reason, atomic=True)
        self.store.audit(member.id, "Botで付与" if add else "Botで解除", str(role.id), actor)

    def operator(self, member):
        return member.guild_permissions.administrator or any(
            role.id in self.config.operators for role in member.roles
        )

    def register_commands(self):
        @self.tree.command(name="交流支援終了", description="交流支援を終了します", guild=self.target)
        async def graduate(interaction: discord.Interaction):
            await self.graduation_command(interaction, interaction.user)

        @self.tree.command(name="メンバーの交流支援終了", description="指定したメンバーの交流支援を終了させます", guild=self.target)
        async def operator_graduate(interaction: discord.Interaction, member: discord.Member):
            if not self.operator(interaction.user):
                await interaction.response.send_message("運営権限が必要です。", ephemeral=True)
                return
            await self.graduation_command(interaction, member)

        @self.tree.command(name="活動記録", description="テキスト投稿、VC参加回数、滞在時間を表示します", guild=self.target)
        async def stats(interaction: discord.Interaction, member: discord.Member | None = None):
            member = member or interaction.user
            if member.id != interaction.user.id and not self.operator(interaction.user):
                await interaction.response.send_message("他のメンバーの照会には運営権限が必要です。", ephemeral=True)
                return
            if self.voice_online:
                self.voice.tick(time.monotonic(), member.id)
            activity = self.store.activity(member.id)
            hours, rest = divmod(int(activity.seconds), HOUR)
            minutes, seconds = divmod(rest, 60)
            await interaction.response.send_message(
                f"<@{member.id}> の記録\n投稿 {activity.posts} 件 / VC {activity.visits} 回 / "
                f"{hours} 時間 {minutes} 分 {seconds} 秒",
                ephemeral=True,
            )

        @self.tree.command(name="記録修正", description="指定した活動記録を新しい合計値に設定します", guild=self.target)
        async def correct(
            interaction: discord.Interaction, member: discord.Member,
            posts: app_commands.Range[int, 0, 2**53 - 1] | None = None,
            visits: app_commands.Range[int, 0, 2**53 - 1] | None = None,
            seconds: app_commands.Range[int, 0, 2**53 - 1] | None = None,
        ):
            if not interaction.user.guild_permissions.administrator or member.bot:
                await interaction.response.send_message("管理者権限と人間のメンバーが必要です。", ephemeral=True)
                return
            if posts is None and visits is None and seconds is None:
                await interaction.response.send_message("修正項目を1つ以上指定してください。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self.lock(member.id):
                if self.voice_online:
                    self.voice.tick(time.monotonic(), member.id)
                self.store.correct(member.id, interaction.user.id, posts=posts, visits=visits, seconds=seconds)
                self.consider(member)
            await interaction.followup.send("記録を修正しました。ロール判定は順次行います。", ephemeral=True)

        @self.tree.command(name="ロール履歴", description="ロール変更と記録修正の直近10件を表示します", guild=self.target)
        async def history(interaction: discord.Interaction, member: discord.Member):
            if not self.operator(interaction.user):
                await interaction.response.send_message("運営権限が必要です。", ephemeral=True)
                return
            rows = self.store.history(member.id)
            lines = [
                f"<t:{int(at)}:f> {action} {detail}" + (f" / 操作者 {actor}" if actor else "")
                for at, actor, action, detail in rows
            ]
            await interaction.response.send_message(("\n".join(lines) or "履歴はありません。")[:2000], ephemeral=True)

        @self.tree.error
        async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            log.error("コマンド実行エラー: %s", error, exc_info=error)
            message = "処理できませんでした。Bot のログを確認してください。"
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def graduation_command(self, interaction, member):
        if not self.target_member(member):
            await interaction.response.send_message("対象メンバーを確認してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock(member.id):
            if self.config.dry_run:
                await self.reconcile(member, actor=interaction.user.id)
                message = "テストモードで交流支援終了判定を確認しました。"
            else:
                self.store.request(member.id, interaction.user.id)
                try:
                    await self.reconcile(member)
                except (discord.HTTPException, RuntimeError) as error:
                    self.retry_at[member.id] = time.monotonic() + 60
                    log.warning("交流支援終了申請を保留: user=%s %s", member.id, error)
                    message = "交流支援終了申請を保存しました。ロールを操作できる状態になると再試行します。"
                else:
                    message = "交流支援終了処理が完了しました。"
        await interaction.followup.send(message, ephemeral=True)
