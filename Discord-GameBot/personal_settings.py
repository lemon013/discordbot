"""Private, owner-bound personal settings; all preferences live in levels.sqlite3."""
import asyncio
import logging
import math

import discord
from discord import app_commands

from preferences import VOICE_DEFAULTS
from voice_catalog import VOICES, voice_info
from sound_common import SoundError

log = logging.getLogger(__name__)
CREDIT = 'クレジットはBotのプロフィール欄を参照してください。'


def parse_speed(value):
    try:
        speed = float(value.strip())
    except ValueError:
        raise SoundError('速度は0.5〜2.0の数値で入力してください。') from None
    if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise SoundError('速度は0.5〜2.0の範囲で入力してください。')
    return speed


class Settings:
    def __init__(self, reading):
        self.reading, self.store = reading, reading.store

    async def show(self, interaction):
        personal, voice = await asyncio.to_thread(self.snapshot, interaction.user.id)
        embed = discord.Embed(title='⚙️ 個人設定', color=discord.Color.blue(),
            description='変更は自動保存され、再起動後も残ります。参加するサーバー共通の個人設定です。')
        label = VOICES.get(str(voice['voice']), str(voice['voice']))
        embed.add_field(name='読み上げ音声', value=label, inline=False)
        embed.add_field(name='読み上げ速度', value=f'{voice["speed"]:g}倍')
        embed.add_field(name='自分の発言の読み上げ', value='ON' if personal['reading_enabled'] else 'OFF')
        embed.add_field(name='レベルアップ通知', value='ON' if personal['level_notifications'] else 'OFF')
        embed.add_field(name='設定の適用範囲', value='読み上げは /voice join で開始中のVCに適用されます。\n'
            '通知をOFFにしてもXP・ジュエル・ロール報酬は獲得できます。\n'
            '通知ONでもサーバー側で無効にされている場合は通知されません。', inline=False)
        info = voice_info(str(voice['voice']))
        embed.set_footer(text=CREDIT + ('\n' + info['credit'] if info else ''))
        view = SettingsView(self, interaction.user.id, personal)
        view.message = await interaction.edit_original_response(content=None, embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none())

    def snapshot(self, uid):
        return self.store.personal(uid), self.reading.preference(uid)

    async def set_voice(self, uid, changes):
        return await asyncio.to_thread(self.store.update, 'user', uid, changes, VOICE_DEFAULTS)


class OwnedView(discord.ui.View):
    def __init__(self, service, owner):
        super().__init__(timeout=180)
        self.service, self.owner, self.message = service, owner, None

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner:
            return True
        await interaction.response.send_message('自分の /settings から変更してください。', ephemeral=True)
        return False

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        log.warning('Personal settings operation failed (error_type=%s)', type(error).__name__)
        text = str(error) if isinstance(error, SoundError) else '設定を変更できませんでした。/settings から再度お試しください。'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class SettingsView(OwnedView):
    def __init__(self, service, owner, personal):
        super().__init__(service, owner)
        self.reading_toggle.label = '自分の読み上げ：' + ('ON' if personal['reading_enabled'] else 'OFF')
        self.notify_toggle.label = 'レベルアップ通知：' + ('ON' if personal['level_notifications'] else 'OFF')

    async def toggle(self, interaction, key):
        await interaction.response.defer()
        # The displayed state is the user's intended target even if another panel was opened.
        label = self.reading_toggle.label if key == 'reading_enabled' else self.notify_toggle.label
        await asyncio.to_thread(self.service.store.update, 'personal', self.owner, {key: not label.endswith('ON')})
        await self.service.show(interaction)
        self.stop()

    @discord.ui.button(label='読み上げ音声を選ぶ', style=discord.ButtonStyle.primary, row=0)
    async def voice(self, interaction, button):
        await interaction.response.defer()
        catalog = await asyncio.wait_for(self.service.reading.providers.voices('voicevox'), 5)
        if not catalog:
            raise SoundError('利用できる音声がありません。時間をおいて再度お試しください。')
        view = VoiceView(self.service, self.owner, catalog)
        view.message = await interaction.edit_original_response(embed=None,
            content='読み上げ音声を選んでください。\n' + CREDIT, view=view,
            allowed_mentions=discord.AllowedMentions.none())
        self.stop()

    @discord.ui.button(label='読み上げ速度を変更', row=0)
    async def speed(self, interaction, button):
        await interaction.response.send_modal(SpeedModal(self.service, self.owner, self))

    @discord.ui.button(label='自分の読み上げ：ON', row=1)
    async def reading_toggle(self, interaction, button):
        await self.toggle(interaction, 'reading_enabled')

    @discord.ui.button(label='レベルアップ通知：ON', row=1)
    async def notify_toggle(self, interaction, button):
        await self.toggle(interaction, 'level_notifications')


class VoiceSelect(discord.ui.Select):
    def __init__(self, view):
        entries = list(view.catalog.items())[view.page * 25:(view.page + 1) * 25]
        super().__init__(placeholder=f'音声を選択（{view.page + 1}/{view.pages}ページ）', row=0,
            options=[discord.SelectOption(label=label[:100], value=str(key)) for key, label in entries])

    async def callback(self, interaction):
        await interaction.response.defer()
        view = self.view
        selected = self.values[0]
        if selected not in view.catalog:
            raise SoundError('一覧から音声を選び直してください。')
        await view.service.set_voice(view.owner, dict(provider='voicevox', voice=selected))
        await view.service.show(interaction)
        view.stop()


class VoiceView(OwnedView):
    def __init__(self, service, owner, catalog, page=0):
        super().__init__(service, owner)
        self.catalog = {str(key): value for key, value in catalog.items()}
        self.pages = (len(catalog) + 24) // 25
        self.page = max(0, min(page, self.pages - 1))
        self.add_item(VoiceSelect(self))
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page == self.pages - 1

    async def move(self, interaction, delta):
        await interaction.response.defer()
        view = VoiceView(self.service, self.owner, self.catalog, self.page + delta)
        view.message = await interaction.edit_original_response(view=view)
        self.stop()

    @discord.ui.button(label='前のページ', row=1)
    async def previous(self, interaction, button):
        await self.move(interaction, -1)

    @discord.ui.button(label='次のページ', row=1)
    async def next(self, interaction, button):
        await self.move(interaction, 1)

    @discord.ui.button(label='設定に戻る', row=1)
    async def back(self, interaction, button):
        await interaction.response.defer()
        await self.service.show(interaction)
        self.stop()


class SpeedModal(discord.ui.Modal, title='読み上げ速度'):
    value = discord.ui.TextInput(label='速度（0.5〜2.0倍）', placeholder='例：1.2', max_length=12)

    def __init__(self, service, owner, parent=None):
        super().__init__(timeout=180)
        self.service, self.owner, self.parent = service, owner, parent

    async def on_submit(self, interaction):
        if interaction.user.id != self.owner:
            await interaction.response.send_message('自分の /settings から変更してください。', ephemeral=True)
            return
        try:
            speed = parse_speed(str(self.value))
        except SoundError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer()
        await self.service.set_voice(self.owner, dict(speed=speed))
        if self.parent:
            self.parent.stop()
        await self.service.show(interaction)

    async def on_error(self, interaction, error):
        await OwnedView.on_error(self, interaction, error, None)


def register_personal_settings(bot, reading):
    service = Settings(reading)

    @bot.tree.command(name='settings', description='自分の読み上げ音声・速度・通知をパネルで設定します')
    @app_commands.guild_only()
    async def settings(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await service.show(interaction)

    return service
