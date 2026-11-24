"""Administrator command for deleting one user's messages from a text channel."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("purgeuser")

# A channel stays reserved while its confirmation is pending and while its job runs.
_active_channel_ids: set[int] = set()
_BULK_DELETE_LIMIT = 100
_PROGRESS_INTERVAL_SECONDS = 10


def _progress_embed(
    target: discord.User | discord.Member,
    scanned: int,
    matched: int,
    deleted: int,
    failed: int,
    *,
    finished: bool = False,
    note: str | None = None,
) -> discord.Embed:
    title = "削除完了" if finished else "メッセージ削除中"
    embed = discord.Embed(title=title, color=discord.Color.green() if finished and not failed else discord.Color.orange())
    embed.add_field(name="対象ユーザー", value=f"{target.mention} (`{target.id}`)", inline=False)
    embed.add_field(name="読み込み済み", value=f"{scanned:,}件", inline=True)
    embed.add_field(name="対象メッセージ", value=f"{matched:,}件", inline=True)
    embed.add_field(name="削除成功", value=f"{deleted:,}件", inline=True)
    embed.add_field(name="削除失敗", value=f"{failed:,}件", inline=True)
    if note:
        embed.add_field(name="状態", value=note[:1024], inline=False)
    return embed


class PurgeUserConfirmView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        target: discord.User | discord.Member,
        channel: discord.TextChannel,
    ) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.target = target
        self.channel = channel
        self.started = False

    async def on_timeout(self) -> None:
        if not self.started:
            _active_channel_ids.discard(self.channel.id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("この確認ボタンを操作できるのはコマンド実行者だけです。", ephemeral=True)
            return False
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("メッセージの管理権限が必要です。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="削除を開始", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.started:
            return await interaction.response.send_message("この削除処理はすでに開始されています。", ephemeral=True)

        bot_member = self.channel.guild.me
        if bot_member is None or not self.channel.permissions_for(bot_member).manage_messages:
            _active_channel_ids.discard(self.channel.id)
            self.stop()
            return await interaction.response.send_message(
                "Botにこのチャンネルの「メッセージの管理」権限がありません。", ephemeral=True
            )

        self.started = True
        self.stop()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            status_message = await self.channel.send(
                embed=_progress_embed(self.target, 0, 0, 0, 0, note="全履歴を確認しています。")
            )
        except discord.HTTPException:
            _active_channel_ids.discard(self.channel.id)
            log.exception("Could not create /purgeuser progress message")
            return await interaction.followup.send(
                "進捗メッセージを投稿できないため、削除を開始できませんでした。", ephemeral=True
            )

        asyncio.create_task(
            _run_purge(self.target, self.channel, status_message),
            name=f"purgeuser:{self.channel.id}:{self.target.id}",
        )
        await interaction.followup.send(
            f"削除処理を開始しました。進捗は {self.channel.mention} のBotメッセージに表示されます。",
            ephemeral=True,
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _active_channel_ids.discard(self.channel.id)
        self.stop()
        await interaction.response.edit_message(content="削除をキャンセルしました。", embed=None, view=None)


async def _run_purge(
    target: discord.User | discord.Member,
    channel: discord.TextChannel,
    status_message: discord.Message,
) -> None:
    scanned = matched = deleted = failed = 0
    recent_batch: list[discord.Message] = []
    cutoff = discord.utils.utcnow() - timedelta(days=14)
    last_update = 0.0
    note: str | None = "全履歴を確認しています。"

    async def update_status(*, force: bool = False) -> None:
        nonlocal last_update
        now = time.monotonic()
        if not force and now - last_update < _PROGRESS_INTERVAL_SECONDS:
            return
        try:
            await status_message.edit(embed=_progress_embed(target, scanned, matched, deleted, failed, note=note))
            last_update = now
        except discord.HTTPException:
            log.exception("Could not update /purgeuser progress message")

    async def delete_individually(messages: list[discord.Message]) -> None:
        nonlocal deleted, failed
        for message in messages:
            try:
                await message.delete()
                deleted += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                failed += 1
                log.warning("Could not delete message %s in channel %s", message.id, channel.id, exc_info=True)
            await update_status()

    async def flush_recent_batch() -> None:
        nonlocal deleted, failed
        if not recent_batch:
            return
        batch = recent_batch[:]
        recent_batch.clear()
        if len(batch) == 1:
            await delete_individually(batch)
            return
        try:
            await channel.delete_messages(batch, reason=f"/purgeuser: requested by channel administrator; target={target.id}")
            deleted += len(batch)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Bulk errors can be caused by one message in the batch; retry each item so
            # remaining deletable messages are still processed and counts stay accurate.
            log.warning("Bulk deletion failed in channel %s; retrying individually", channel.id, exc_info=True)
            await delete_individually(batch)
        await update_status()

    try:
        # limit=None deliberately walks the complete message history using Discord's
        # paginated history endpoint; bot.max_messages does not limit this iterator.
        async for message in channel.history(limit=None, oldest_first=False):
            scanned += 1
            if message.author.id != target.id:
                continue
            matched += 1
            if message.created_at > cutoff:
                recent_batch.append(message)
                if len(recent_batch) == _BULK_DELETE_LIMIT:
                    await flush_recent_batch()
            else:
                await flush_recent_batch()
                await delete_individually([message])
            await update_status()

        await flush_recent_batch()
    except Exception as exc:
        note = f"履歴の読み込み中にエラーが発生しました（{type(exc).__name__}）。読み込み済みの対象は処理しました。"
        log.exception("History scan failed for /purgeuser in channel %s", channel.id)
    finally:
        try:
            await status_message.edit(
                embed=_progress_embed(target, scanned, matched, deleted, failed, finished=True, note=note or "全履歴の確認が完了しました。")
            )
        except discord.HTTPException:
            log.exception("Could not publish /purgeuser completion status")
        _active_channel_ids.discard(channel.id)


def register_purgeuser(bot: commands.Bot) -> None:
    @bot.tree.command(name="purgeuser", description="指定ユーザーのチャンネル内メッセージをすべて削除します")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(user="削除対象のユーザー", channel="履歴を削除するテキストチャンネル")
    async def purgeuser(
        interaction: discord.Interaction,
        user: discord.User,
        channel: discord.TextChannel,
    ) -> None:
        if interaction.guild_id != channel.guild.id:
            return await interaction.response.send_message("このサーバー内のチャンネルを指定してください。", ephemeral=True)
        if channel.id in _active_channel_ids:
            return await interaction.response.send_message("このチャンネルでは、すでに削除処理または確認待ちがあります。", ephemeral=True)

        bot_member = channel.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member else None
        if permissions is None or not permissions.view_channel or not permissions.read_message_history or not permissions.manage_messages:
            return await interaction.response.send_message(
                "Botに「チャンネルを見る」「メッセージ履歴を読む」「メッセージの管理」の権限が必要です。",
                ephemeral=True,
            )

        _active_channel_ids.add(channel.id)
        view = PurgeUserConfirmView(interaction.user.id, user, channel)
        try:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="メッセージ削除の確認",
                    description=(
                        f"{user.mention} の {channel.mention} 内の全メッセージを削除します。\n"
                        "14日未満のメッセージは最大100件ずつ、14日以上前は1件ずつ削除します。\n"
                        "実行後は取り消せません。続行しますか？"
                    ),
                    color=discord.Color.red(),
                ),
                view=view,
                ephemeral=True,
            )
        except Exception:
            _active_channel_ids.discard(channel.id)
            raise
