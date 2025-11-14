import os
import re
import asyncio
import random
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, List

import discord
from discord import app_commands
from discord.ext import commands

EMOJI_JOIN = "🎉"
ALLOW_CASE_INSENSITIVE = True
MATCH_MODE = "equals"

_TIME_RE = re.compile(
    r"(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?")


def parse_duration(s: str) -> int:
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    m = _TIME_RE.fullmatch(s)
    if not m:
        raise ValueError("時間格式錯誤，請用例如 1h30m / 45m / 10s / 1d2h")
    days = int(m.group("d") or 0)
    hours = int(m.group("h") or 0)
    minutes = int(m.group("m") or 0)
    seconds = int(m.group("s") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def normalize_text(t: str) -> str:
    return t.lower() if ALLOW_CASE_INSENSITIVE else t


def match_phrase(message_content: str, phrase: str) -> bool:
    a = normalize_text(message_content.strip())
    b = normalize_text(phrase.strip())
    if MATCH_MODE == "contains":
        return b in a
    return a == b


@dataclass
class Giveaway:
    guild_id: int
    channel_id: int
    message_id: int
    prize: str
    winners: int
    host_id: int
    ends_at_unix: float
    must_said: str
    said_users: Set[int] = field(default_factory=set)
    reacted_users: Set[int] = field(default_factory=set)
    ended: bool = False


class GiveawayBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.guilds = True
        intents.reactions = True
        intents.messages = True

        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )
        self.giveaways: Dict[int, Giveaway] = {}

    async def setup_hook(self):
        await self.tree.sync()

    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        content = message.content or ""
        if not content.strip():
            return
        for g in list(self.giveaways.values()):
            if g.guild_id != message.guild.id or g.ended:
                continue
            if match_phrase(content, g.must_said):
                g.said_users.add(message.author.id)
        await self.process_commands(message)

    async def on_raw_reaction_add(self,
                                  payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != EMOJI_JOIN:
            return
        g = self.giveaways.get(payload.message_id)
        if not g or g.ended:
            return
        if payload.user_id == self.user.id:
            return
        g.reacted_users.add(payload.user_id)

    async def _end_giveaway(self,
                            message_id: int,
                            force: bool = False) -> Optional[List[int]]:
        g = self.giveaways.get(message_id)
        if not g or g.ended:
            return None
        g.ended = True

        guild = self.get_guild(g.guild_id)
        channel = guild.get_channel(g.channel_id) if guild else None

        eligible_ids: Set[int] = set()
        if guild is not None:
            for uid in g.reacted_users:
                member = guild.get_member(uid)
                if member and not member.bot and uid in g.said_users:
                    eligible_ids.add(uid)

        winner_ids: List[int] = []
        pool = list(eligible_ids)
        if len(pool) == 0:
            text = f"🎁 **{g.prize}** 抽獎結束！沒有符合資格的參加者（需發言「{g.must_said}」並按 {EMOJI_JOIN}）。"
            if channel:
                await channel.send(text)
        else:
            k = min(g.winners, len(pool))
            winner_ids = random.sample(pool, k=k)
            mentions = " ".join(f"<@{uid}>" for uid in winner_ids)
            text = (f"🎁 **{g.prize}** 抽獎結束！\n"
                    f"得獎者：{mentions}\n"
                    f"條件：在任一頻道說過「{g.must_said}」且對抽獎貼文按 {EMOJI_JOIN}。")
            if channel:
                await channel.send(text)
        try:
            if channel:
                msg = await channel.fetch_message(g.message_id)
                if msg.embeds:
                    e = msg.embeds[0]
                    ended_embed = discord.Embed(title=e.title or "🎉 抽獎",
                                                description=e.description
                                                or "",
                                                color=discord.Color.red())
                    for f in e.fields:
                        ended_embed.add_field(name=f.name,
                                              value=f.value,
                                              inline=f.inline)
                    ended_embed.set_footer(text="抽獎已結束")
                    await msg.edit(embed=ended_embed)
        except Exception:
            pass

        return winner_ids

    async def _countdown_and_end(self, message_id: int):
        g = self.giveaways.get(message_id)
        if not g or g.ended:
            return
        delay = max(0, g.ends_at_unix - discord.utils.utcnow().timestamp())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._end_giveaway(message_id)


bot = GiveawayBot()


@bot.tree.command(name="gstart", description="開始抽獎")
@app_commands.describe(duration="持續時間（例如：30m / 2h / 1h30m / 1d2h）",
                       prize="獎品名稱",
                       winners="得獎人數（預設 1）",
                       must_said="必須在伺服器任何頻道說出的訊息",
                       channel="抽獎要發布的頻道（預設當前頻道）")
async def gstart(interaction: discord.Interaction,
                 duration: str,
                 prize: str,
                 winners: app_commands.Range[int, 1, 50] = 1,
                 must_said: str = "",
                 channel: Optional[discord.TextChannel] = None):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("需要 **管理伺服器** 權限才能抽。",
                                                       ephemeral=True)

    if not must_said.strip():
        return await interaction.response.send_message(
            "請提供條件內容（例：我愛貓貓）。", ephemeral=True)

    try:
        seconds = parse_duration(duration)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)

    ch = channel or interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("請在文字頻道使用，或指定一個文字頻道。",
                                                       ephemeral=True)

    ends_at = discord.utils.utcnow().timestamp() + seconds

    
    host = interaction.user
    e = discord.Embed(
        title="🎉 抽獎開始！",
        description=
        (f"獎品：**{prize}**\n"
         f"主辦：{host.mention}\n"
         f"得獎人數：**{winners}**\n"
         f"結束時間：<t:{int(ends_at)}:f>（<t:{int(ends_at)}:R>）\n"
         f"參加方式：對此訊息按 {EMOJI_JOIN}\n"
         f"資格限制：在任一頻道說過「`{must_said}`」\n"
         ),
        color=discord.Color.random(),
    )
    e.set_footer(text="祝你好運！")

    await interaction.response.defer(ephemeral=True, thinking=True)
    msg = await ch.send(embed=e)
    try:
        await msg.add_reaction(EMOJI_JOIN)
    except Exception:
        pass

    g = Giveaway(
        guild_id=ch.guild.id,
        channel_id=ch.id,
        message_id=msg.id,
        prize=prize,
        winners=winners,
        host_id=host.id,
        ends_at_unix=ends_at,
        must_said=must_said,
    )
    bot.giveaways[msg.id] = g
    bot.loop.create_task(bot._countdown_and_end(msg.id))

    await interaction.followup.send(f"已在 {ch.mention} 建立抽獎（訊息 ID：`{msg.id}`）。",
                                    ephemeral=True)


@bot.tree.command(name="gend", description="提前結束抽獎")
@app_commands.describe(message_id="抽獎訊息的 ID")
async def gend(interaction: discord.Interaction, message_id: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message(
            "需要 **管理伺服器** 權限才能結束抽獎。", ephemeral=True)

    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("message_id 應為數字。",
                                                       ephemeral=True)

    res = await bot._end_giveaway(mid, force=True)
    if res is None:
        return await interaction.response.send_message("找不到該抽獎或已結束。",
                                                       ephemeral=True)
    await interaction.response.send_message("已結束該抽獎。", ephemeral=True)


@bot.tree.command(name="greroll", description="重抽")
@app_commands.describe(message_id="抽獎訊息的 ID", winners="要抽出的名額（預設 1）")
async def greroll(interaction: discord.Interaction,
                  message_id: str,
                  winners: app_commands.Range[int, 1, 50] = 1):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("需要 **管理伺服器** 權限才能重抽。",
                                                       ephemeral=True)

    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("message_id 應為數字。",
                                                       ephemeral=True)

    g = bot.giveaways.get(mid)
    if not g:
        return await interaction.response.send_message(
            "找不到該抽獎", ephemeral=True)

    guild = bot.get_guild(g.guild_id)
    if guild is None:
        return await interaction.response.send_message("找不到伺服器資訊。",
                                                       ephemeral=True)

    eligible_ids = []
    for uid in g.reacted_users:
        member = guild.get_member(uid)
        if member and not member.bot and uid in g.said_users:
            eligible_ids.append(uid)

    if not eligible_ids:
        return await interaction.response.send_message("目前沒有符合資格的參加者可以重抽。",
                                                       ephemeral=True)

    k = min(winners, len(eligible_ids))
    win = random.sample(eligible_ids, k=k)
    mentions = " ".join(f"<@{uid}>" for uid in win)
    await interaction.response.send_message(f"重抽結果：{mentions}",
                                            ephemeral=False)

@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(name="Made by @kusanagi_akane"))
    print(f"登入 [{bot.user}]\nMade by @kusanagi_akane")
    print("------")
if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN") or ""
    if not TOKEN:
        print("找不到環境變數")
    else:
        bot.run(TOKEN)
