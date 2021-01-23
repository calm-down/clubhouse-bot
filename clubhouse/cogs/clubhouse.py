import asyncio
from asyncio import Lock
from datetime import datetime, timedelta
from functools import cmp_to_key
from os import getenv
from re import match
from typing import Optional, Union, List, Dict, Tuple

import discord
import sentry_sdk
from PyDrocsid.database import db_thread, db
from PyDrocsid.emojis import name_to_emoji
from PyDrocsid.events import StopEventHandling
from PyDrocsid.translations import translations
from PyDrocsid.util import send_long_embed
from discord import Message, Role, PartialEmoji, TextChannel, Member, NotFound, Embed, HTTPException, Forbidden, Guild, \
    CategoryChannel, PermissionOverwrite, ChannelType, Status
from discord.ext import commands, tasks
from discord.ext.commands import Cog, Bot, guild_only, Context
from discord.utils import snowflake_time
from sqlalchemy import or_

from colours import Colours
from models.category import Category
from models.channel import Channel
from models.donator import Donator
from models.searcher import Searcher
from models.state import State
from util import get_prefix

start_message_link = getenv("MESSAGE_LINK")
team_role_id = getenv("TEAM_ROLE_ID")
team_channel_id = getenv("TEAM_CHANNEL_ID")

lst = start_message_link.split("/")
if not len(lst) == 7 or not lst[-2].isnumeric() or not lst[-1].isnumeric():
    print("start message link is invalid")

start_channel_id = int(lst[-2])
start_message_id = int(lst[-1])

if not team_role_id.isnumeric():
    print("ERROR: team role id should be a number")
    exit(1)
if not team_channel_id.isnumeric():
    print("ERROR: team channel id should be a number")
    exit(1)

team_channel_id = int(team_channel_id)
team_role_id = int(team_role_id)

gift = name_to_emoji["gift"]
mag = name_to_emoji["mag"]
channel_lock = Lock()
queue_lock = Lock()


class Clubhouse(Cog, name="Clubhouse"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.team_channel: Union[TextChannel, None] = None
        self.guild: Optional[Guild] = None
        self.team_role: Optional[Role] = None
        self.task_set: set = set()
        self.start_message: Optional[Message] = None

    async def on_ready(self):
        self.guild: Optional[Guild] = self.bot.guilds[0]
        self.team_channel = self.guild.get_channel(team_channel_id)
        if self.team_channel is None:
            print("Unable to find team channel")
            exit(1)
        self.team_role = self.guild.get_role(team_role_id)
        if self.team_role is None:
            print("Unable to find team role")
            exit(1)

        categories: List[CategoryChannel] = self.guild.categories
        db_categories: Dict[int, Category] = {x.category_id: x for x in await db_thread(db.all, Category)}
        found_categories: List[int] = []
        for category in categories:
            if category.name == "Vermittlung":
                if category.id not in db_categories:
                    await db_thread(Category.create, category_id=category.id)
                found_categories.append(category.id)
        for category_id in set(db_categories.keys()).difference(set(found_categories)):
            await db_thread(db.delete, db_categories.get(category_id))
            del db_categories[category_id]

        try:
            if found_categories == 0:
                category: CategoryChannel = await self.guild.create_category("Vermittlung")
                await db_thread(Category.create, category.id)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            print("Could not create category channel")
            exit(1)

        start_channel: Optional[TextChannel] = self.guild.get_channel(start_channel_id)
        if start_channel is None:
            print("Unable to find start channel")
            exit(1)

        self.start_message: Optional[Message] = await start_channel.fetch_message(start_message_id)
        if self.start_message is None:
            print("Unable to find start message in start channel")
            exit(1)

        await self.start_message.add_reaction(gift)
        await self.start_message.add_reaction(mag)

        try:
            self.inactive_loop.start()
        except RuntimeError:
            self.inactive_loop.restart()
        try:
            self.inactive_channel_deleter_loop.start()
        except RuntimeError:
            self.inactive_channel_deleter_loop.restart()
        try:
            self.inactive_channel_reminder_loop.start()

        except RuntimeError:
            self.inactive_channel_reminder_loop.restart()

    @tasks.loop(hours=2)
    async def inactive_channel_reminder_loop(self):
        # if last message (ignore bot and team messages) was longer than 2 hours ago
        # send message in channel translations.close_channel_reminder
        categories: List[Category] = await db_thread(db.all, Category)
        for category in categories:
            category_channel: Optional[CategoryChannel] = self.bot.get_channel(category.category_id)
            if category_channel is None:
                await db_thread(db.delete, category)
                continue
            for channel in category_channel.channels:
                if channel.type != ChannelType.text:
                    continue
                channel: TextChannel = channel
                if datetime.utcnow() < channel.created_at + timedelta(hours=2):
                    continue
                async for f in channel.history(oldest_first=False, after=datetime.utcnow() - timedelta(hours=2),
                                               limit=100):
                    if f.author.id == self.bot.user.id:
                        break
                    if not f.author.bot:
                        break
                else:
                    f = None
                if f is None or datetime.utcnow() >= snowflake_time(f.id) + timedelta(hours=2):
                    try:
                        db_channel: Optional[Channel] = await db_thread(db.get, Channel, channel.id)
                        if db_channel:
                            await channel.send(
                                translations.f_close_channel_reminder(db_channel.donator_id, db_channel.searcher_id))
                    except Exception as e:
                        sentry_sdk.capture_exception(e)

    @tasks.loop(minutes=30)
    async def inactive_channel_deleter_loop(self):
        # if last message (ignore bot messages) was longer than 8 hours ago
        change = False
        categories: List[Category] = await db_thread(db.all, Category)
        for category in categories:
            category_channel: Optional[CategoryChannel] = self.bot.get_channel(category.category_id)
            if category_channel is None:
                await db_thread(db.delete, category)
                continue
            for channel in category_channel.channels:
                if channel.type != ChannelType.text:
                    continue
                channel: TextChannel = channel
                if datetime.utcnow() < channel.created_at + timedelta(hours=24):
                    continue
                async for f in channel.history(oldest_first=False, after=datetime.utcnow() - timedelta(hours=24),
                                               limit=100):
                    if not f.author.bot:
                        break
                else:
                    f = None
                if f is None or datetime.utcnow() >= snowflake_time(f.id) + timedelta(hours=24):
                    overwrites = channel.overwrites
                    db_channel: Optional[Channel] = await db_thread(db.get, Channel, channel.id)
                    if db_channel:
                        users_to_notify: List[discord.Member] = [
                            u for u, o in overwrites.items()
                            if isinstance(u, discord.Member) and u.id in [db_channel.searcher_id, db_channel.donator_id]
                        ]
                        for user in users_to_notify:
                            db_entry: Optional[Searcher] = await db_thread(db.get, Searcher, user.id)
                            if db_entry:
                                await db_thread(Searcher.change_state, user.id, State.QUEUED)

                            db_entry: Optional[Donator] = await db_thread(db.get, Donator, user.id)
                            if db_entry:
                                await db_thread(Donator.change_state, user.id, State.MATCHED)
                                await db_thread(Donator.change_used_invites, user.id,
                                                max(0, db_entry.used_invites - 1))
                            await self.send_dm_text(user, translations.channel_timed_out)
                            change = True
                        try:
                            await channel.delete()
                            await db_thread(db.delete, db_channel)
                        except Exception as e:
                            sentry_sdk.capture_exception(e)
            if change:
                await self.pair()

    @tasks.loop(minutes=5)
    async def inactive_loop(self):
        donators: List[Donator] = await db_thread(db.all, Donator, state=State.INITIAL)
        for donator in donators:
            if datetime.utcnow() >= donator.last_contact + timedelta(minutes=5):
                await self.send_dm_text(self.bot.get_user(donator.user_id), translations.gift_reminder)

    async def remove_from_queue(self, data: tuple, locked: bool = False):
        if locked:
            try:
                self.task_set.remove(data)
            except:
                pass
        else:
            async with queue_lock:
                try:
                    self.task_set.remove(data)
                except:
                    pass

    async def put_in_queue(self, data, locked: bool = False):
        if locked:
            try:
                self.task_set.add(data)
            except:
                pass
        else:
            async with queue_lock:
                try:
                    self.task_set.add(data)
                except:
                    pass

    async def search_in_queue(self, data, locked: bool = False):
        if locked:
            try:
                if data in self.task_set:
                    return True
                return False
            except:
                pass
        else:
            async with queue_lock:
                try:
                    if data in self.task_set:
                        return True
                    return False
                except:
                    return False

    async def send_dm_text(self, user: Union[discord.User, discord.Member], text: str) -> bool:
        data = (user.id, text)
        async with queue_lock:
            if await self.search_in_queue(data, True):
                return False
            await self.put_in_queue(data, True)
        await asyncio.sleep(0.00001)
        while True:
            try:
                await user.send(text)
            except Forbidden:
                await self.remove_from_queue(data)
                await self.team_channel.send(translations.f_no_dm(user.mention))
                return False
            except HTTPException as e:
                if e.status != 429:
                    await self.remove_from_queue(data)
                    await self.team_channel.send(f"HTTP Error {e.status}! Check sentry!")
                    raise e
            except Exception as e:
                await self.remove_from_queue(data)
                raise e
            else:
                await self.remove_from_queue(data)
                return True
            await asyncio.sleep(5)

    async def send_dm_embed(self, user: Union[discord.User, discord.Member], embed: Embed) -> bool:
        data = (user.id, embed.description)
        async with queue_lock:
            if await self.search_in_queue(data, True):
                return False
            await self.put_in_queue(data, True)
        await asyncio.sleep(0.00001)
        while True:
            try:
                await user.send(embed=embed)
            except Forbidden:
                await self.remove_from_queue(data)
                await self.team_channel.send(translations.f_no_dm(user.mention))
                return False
            except HTTPException as e:
                if e.status != 429:
                    await self.remove_from_queue(data)
                    await self.team_channel.send(f"HTTP Error {e.status}! Check sentry!")
                    raise e
            except Exception as e:
                await self.remove_from_queue(data)
                raise e
            else:
                await self.remove_from_queue(data)
                return True
            await asyncio.sleep(5)

    async def calculate_queues(self) -> Tuple[List[Searcher], List[Donator]]:
        def sort_users(x: Union[Donator, Searcher, None] = None, y: Union[Donator, Searcher, None] = None) -> int:
            if y is None:
                return -1
            user_x: Optional[discord.Member] = self.guild.get_member(x.user_id)
            user_y: Optional[discord.Member] = self.guild.get_member(y.user_id)
            if user_x.status == Status.offline and user_y.status == Status.offline \
                    or user_x.status != Status.offline and user_y.status != Status.offline:
                if isinstance(x, Donator) and isinstance(y, Donator):
                    return int(x.last_contact.timestamp() - y.last_contact.timestamp())
                elif isinstance(x, Searcher) and isinstance(y, Searcher):
                    return int(x.enqueued_at.timestamp() - y.enqueued_at.timestamp())
            elif user_x.status == Status.offline and user_y.status != Status.offline:
                return 1
            elif user_x.status != Status.offline and user_y.status == Status.offline:
                return -1

        donating_users: List[Donator] = await db_thread(
            lambda: db.query(Donator)
                .filter(Donator.used_invites < Donator.invite_count)
                .filter(Donator.state.in_((State.MATCHED, State.QUEUED)))
                .all())

        searching_users: List[Searcher] = await db_thread(
            lambda: db.query(Searcher).filter_by(state=State.QUEUED).all())

        if donating_users:
            donating_users.sort(key=cmp_to_key(sort_users))
        if searching_users:
            searching_users.sort(key=cmp_to_key(sort_users))

        return searching_users, donating_users

    async def pair(self):
        async with channel_lock:
            searching_users, donating_users = await self.calculate_queues()
            if not donating_users:
                return
            if not searching_users:
                return

            for db_user in searching_users:
                user: Optional[discord.Member] = self.guild.get_member(db_user.user_id)
                if not user:
                    await db_thread(db.delete, db_user)
                    continue
                while len(donating_users) > 0:
                    db_donator = donating_users[0]
                    donator: Optional[discord.Member] = self.guild.get_member(db_donator.user_id)
                    if not donator:
                        del donating_users[0]
                        await db_thread(db.delete, db_donator)
                        continue

                    await self.team_channel.send(translations.f_paired_users(donator.mention, user.mention))

                    overwrites = {
                        self.guild.default_role: PermissionOverwrite(read_messages=False),
                        self.guild.me: PermissionOverwrite(read_messages=True),
                        user: PermissionOverwrite(read_messages=True),
                        donator: PermissionOverwrite(read_messages=True),
                        self.team_role: PermissionOverwrite(read_messages=True)
                    }

                    categories: List[Category] = await db_thread(db.all, Category)
                    for category in categories:
                        category_channel: Optional[CategoryChannel] = self.bot.get_channel(category.category_id)
                        if category_channel is None:
                            await db_thread(db.delete, category)
                            continue
                        if len(category_channel.channels) < 50:
                            new_channel: TextChannel = await category_channel.create_text_channel(f"{user.name}",
                                                                                                  overwrites=overwrites)
                            break
                    else:
                        category: CategoryChannel = await self.guild.create_category("Vermittlung")
                        new_channel: TextChannel = await category.create_text_channel(f"{user.name}",
                                                                                      overwrites=overwrites)
                        await db_thread(Category.create, category.id)

                    await db_thread(Channel.create, channel_id=new_channel.id, donator_id=donator.id,
                                    searcher_id=user.id)
                    await new_channel.send(translations.f_ping_users(donator.mention, user.mention))
                    tutorial_embed = Embed(
                        title=translations.tutorial_embed_title,
                        description=translations.f_tutorial_embed_description(
                            user.mention, donator.mention, colour=Colours.blue)
                    )
                    await new_channel.send(embed=tutorial_embed)

                    await self.send_dm_text(user, translations.f_channel_created(donator.mention, new_channel.mention))
                    await self.send_dm_text(donator, translations.f_channel_created(user.mention, new_channel.mention))

                    if db_donator.invite_count <= db_donator.used_invites + 1:
                        del donating_users[0]
                    await db_thread(db_donator.change_used_invites, db_donator.user_id, db_donator.used_invites + 1)
                    db_donator.used_invites += 1
                    await db_thread(Donator.change_state, donator.id, State.MATCHED)
                    await db_thread(Searcher.change_state, user.id, State.MATCHED)
                    break

    async def on_member_remove(self, member: Member):
        if member.bot:
            return
        for user in [await db_thread(db.get, Donator, member.id), await db_thread(db.get, Searcher, member.id)]:
            if not user:
                continue

            if user.state in [State.INITIAL, State.QUEUED]:
                await db_thread(db.delete, user)
                continue

            if State.completed(user):
                continue

            db_channel = None
            for db_channel in await db_thread(lambda: db.query(Channel).filter(or_(
                    member.id == Channel.donator_id,
                    member.id == Channel.searcher_id
            )).all()):
                donator = await db_thread(db.get, Donator, db_channel.donator_id)
                other_id = 0
                if donator:
                    if member.id == donator.user_id:
                        await db_thread(Donator.change_state, member.id, State.ABORTED)
                    else:
                        other_id = donator.user_id
                    await db_thread(Donator.change_used_invites, donator.user_id,
                                    max(0, donator.used_invites - 1))
                searcher = await db_thread(db.get, Searcher, db_channel.searcher_id)
                if searcher:
                    if member.id == searcher.user_id:
                        await db_thread(Searcher.change_state, searcher.user_id, State.ABORTED)
                    else:
                        await db_thread(Searcher.change_state, searcher.user_id, State.QUEUED)
                        other_id = searcher.user_id
                if other_id != 0 and (other_user := self.bot.get_user(other_id)) is not None:
                    await self.send_dm_text(other_user, translations.other_used_quitted)

                channel: Optional[TextChannel] = self.bot.get_channel(db_channel.channel_id)
                await db_thread(db.delete, db_channel)
                try:
                    if channel:
                        await channel.delete()
                except Exception as e:
                    sentry_sdk.capture_exception(e)
            if db_channel:
                await self.pair()

    async def on_raw_reaction_add(self, message: Message, emoji: PartialEmoji, member: Member):
        if member.bot or message.guild is None:
            return
        if message.id != start_message_id:
            return
        await message.remove_reaction(emoji, member)
        asyncio.get_running_loop().create_task(self.reaction_worker(message, emoji, member))
        raise StopEventHandling

    async def gift_reaction(self, member: Member):
        user: Optional[Searcher] = await db_thread(db.get, Searcher, member.id)
        ret = True
        if user and not State.completed(user):
            if user.state == State.INITIAL:
                await self.send_dm_text(member, translations.read_again)
            elif user.state == State.QUEUED:
                await self.send_dm_text(member, translations.self_still_in_queue)
            elif user.state == State.MATCHED:
                ret = False
                await self.send_dm_text(member, translations.self_still_in_queue)
            if ret:
                return
        user = await db_thread(db.get, Donator, member.id)
        if user:
            if user.state == State.INITIAL:
                await self.send_dm_text(member, translations.gift_reminder)
            elif user.state == State.QUEUED:
                await self.send_dm_text(member, translations.self_still_donating)
            elif user.state == State.MATCHED:
                await self.send_dm_text(member, translations.already_in_room)
            elif State.completed(user):
                await self.send_dm_text(member, translations.all_invited_donated)
            return
        embed = Embed(
            title=translations.gift_title,
            description=translations.f_gift_description(member.mention),
            colour=Colours.blue
        )
        if not await self.send_dm_embed(member, embed=embed):
            return
        await db_thread(Donator.create, member.id)

    async def search_reaction(self, member: Member):
        user = await db_thread(db.get, Donator, member.id)
        if user:
            if user.state == State.INITIAL:
                await self.send_dm_text(member, translations.invite_mode)
            else:
                await self.send_dm_text(member, translations.already_invited)
            return
        user = await db_thread(db.get, Searcher, member.id)
        if user and not State.completed(user):
            if user.state == State.INITIAL:
                await self.send_dm_text(member, translations.read_again)
            elif user.state == State.QUEUED:
                await self.send_dm_text(member, translations.self_still_in_queue)
            elif user.state == State.MATCHED:
                await self.send_dm_text(member, translations.already_in_room)
            return

        if user and State.completed(user):
            await self.send_dm_text(member, translations.already_invited)
            return

        embed: discord.Embed = discord.Embed(title=translations.mag_field_name, color=0x1bcc79)
        embed.add_field(name="** **", value=translations.f_mag_field_value(member.mention), inline=False)
        embed.add_field(name="** **", value=translations.mag_field2_value, inline=False)
        embed.add_field(name="** **", value=translations.mag_field3_value, inline=False)

        if not await self.send_dm_embed(member, embed=embed):
            return
        if not user:
            await db_thread(Searcher.create, member.id)

    async def reaction_worker(self, message: Message, emoji: PartialEmoji, member: Member):
        emoji = str(emoji)
        if emoji == gift:
            await self.gift_reaction(member)

        elif emoji == mag:
            await self.search_reaction(member)

    async def on_message(self, message: Message):
        if message.content.startswith(await get_prefix()):
            return
        if message.author.bot:
            return
        user: Union[Donator, Searcher] = await db_thread(db.get, Donator, message.author.id)
        if not user or State.completed(user):
            user = await db_thread(db.get, Searcher, message.author.id)
        if user is None or State.completed(user):
            return

        if message.content.lower() == "exit":
            if isinstance(user, Donator):
                await self.send_dm_text(message.author, translations.stop_donating)

            if isinstance(user, Searcher) and not State.completed(user):
                await self.send_dm_text(message.author, translations.queue_left)
            if user.state in [State.INITIAL, State.QUEUED]:
                await db_thread(db.delete, user)

            if user.state != State.MATCHED:
                return

            db_channel = None
            for db_channel in await db_thread(lambda: db.query(Channel).filter(or_(
                    message.author.id == Channel.donator_id,
                    message.author.id == Channel.searcher_id
            )).all()):
                donator = await db_thread(db.get, Donator, db_channel.donator_id)
                other_id = 0
                if donator:
                    if message.author.id == donator.user_id:
                        await db_thread(Donator.change_state, message.author.id, State.ABORTED)
                    else:
                        other_id = donator.user_id
                    await db_thread(Donator.change_used_invites, donator.user_id,
                                    max(0, donator.used_invites - 1))
                searcher = await db_thread(db.get, Searcher, db_channel.searcher_id)
                if searcher:
                    if message.author.id == searcher.user_id:
                        await db_thread(Searcher.change_state, searcher.user_id, State.ABORTED)
                    else:
                        await db_thread(Searcher.change_state, searcher.user_id, State.QUEUED)
                        other_id = searcher.user_id
                if other_id != 0 and (other_user := self.bot.get_user(other_id)) is not None:
                    await self.send_dm_text(other_user, translations.other_used_quitted)

                channel: Optional[TextChannel] = self.bot.get_channel(db_channel.channel_id)
                await db_thread(db.delete, db_channel)
                try:
                    if channel:
                        await channel.delete()
                except Exception as e:
                    sentry_sdk.capture_exception(e)
            if db_channel:
                await self.pair()
            return

        if message.guild is None:
            if isinstance(user, Donator) and user.state == State.INITIAL:
                matcher = match(r"(\d).*", message.content)
                if len(matcher.groups()) == 0 or not 1 <= int(matcher.groups()[0]) <= 5:
                    await self.send_dm_text(message.author, translations.gift_invalid_input)
                    return
                await db_thread(lambda:
                                (Donator.change_state(user.user_id, State.QUEUED),
                                 Donator.change_invite_count(user.user_id, int(matcher.groups()[0])))
                                )
                await self.send_dm_text(message.author, translations.gift_ready)
                await self.pair()

            else:
                if user.state == State.INITIAL:
                    if message.content.lower() == "apple":
                        await db_thread(Searcher.change_state, user.user_id, State.QUEUED)
                        await db_thread(Searcher.set_timestamp, user.user_id)
                        await self.send_dm_text(message.author, translations.mag_added_queue)
                        await self.pair()
                    else:
                        await self.send_dm_text(message.author, translations.read_again)

    @commands.command(aliases=["c"])
    @guild_only()
    async def close(self, ctx: Context):
        """
        teamler and members of the channel only
        closes channel and marks process as completed
        """
        if ctx.message.author.bot:
            return

        channel: TextChannel = ctx.channel
        user: discord.Member = ctx.author
        overwrite = channel.overwrites.get(user)
        if ((overwrite is None
             or not overwrite.read_messages
        ) and self.team_role not in user.roles):
            await ctx.send(translations.f_chanenl_delete_denied(user.mention))
            return
        if channel.category is None or channel.category.id not in map(
                lambda x: x.category_id, await db_thread(db.all, Category)):
            await ctx.send(translations.f_wrong_channel(user.mention))
            return

        db_channel: Optional[Channel] = await db_thread(db.get, Channel, channel.id)
        if await db_thread(db.get, Searcher, db_channel.searcher_id):
            await db_thread(Searcher.change_state, db_channel.searcher_id, State.DONE)
        donator: Optional[Donator] = await db_thread(db.get, Donator, db_channel.donator_id)
        if donator \
                and donator.used_invites >= donator.invite_count \
                and await db_thread(lambda: db.query(Channel).filter_by(donator_id=donator.user_id).count()) <= 1:
            await db_thread(Donator.change_state, db_channel.donator_id, State.DONE)
        if db_channel:
            users_to_notify: List[discord.Member] = [
                u for u, o in channel.overwrites.items()
                if isinstance(u, discord.Member) and u.id == db_channel.searcher_id
            ]
            if not await db_thread(db.get, Donator, users_to_notify[0].id):
                await self.send_dm_text(users_to_notify[0], translations.invite_user)
        try:
            await db_thread(db.delete, db_channel)
            await channel.delete()
        except (Forbidden, NotFound, HTTPException):
            pass

    @commands.command(aliases=["r"])
    @guild_only()
    async def reset(self, ctx: Context, member: Optional[Member]):
        """
        team only
        resets the database for a user (e.g. clicked on wrong reaction, or was banned from the process)
        """
        if ctx.message.author.bot:
            return

        if not member:
            await ctx.send(translations.member_not_found)
            return

        if self.team_role not in ctx.author.roles:
            await ctx.send(translations.f_permission_denied(ctx.author.mention))
            return
        found = 0
        user: Optional[Donator] = await db_thread(db.get, Donator, member.id)
        if user:
            await db_thread(db.delete, user)
            found += 1
        user: Optional[Searcher] = await db_thread(db.get, Searcher, member.id)
        if user:
            await db_thread(db.delete, user)
            found += 1
        if found == 0:
            await ctx.send(translations.f_user_not_found(member.mention))
            return
        else:
            db_channel = None
            for db_channel in await db_thread(lambda: db.query(Channel).filter(or_(
                    member.id == Channel.donator_id,
                    member.id == Channel.searcher_id
            )).all()):
                donator = await db_thread(db.get, Donator, db_channel.donator_id)
                other_id = 0
                if donator and member.id != donator.user_id:
                    await db_thread(Donator.change_used_invites, donator.user_id, max(0, donator.used_invites - 1))
                    other_id = donator.user_id
                searcher = await db_thread(db.get, Searcher, db_channel.searcher_id)
                if searcher and member.id != searcher.user_id:
                    await db_thread(Searcher.change_state, searcher.user_id, State.QUEUED)
                    other_id = searcher.user_id
                if other_id != 0 and (other_user := self.bot.get_user(other_id)) is not None:
                    await self.send_dm_text(other_user, translations.chanel_was_closed_by_team)

                channel: Optional[TextChannel] = self.bot.get_channel(db_channel.channel_id)
                await db_thread(db.delete, db_channel)
                await ctx.send(translations.f_user_resetted(member.mention))
                try:
                    if channel:
                        await channel.delete()
                except Exception as e:
                    sentry_sdk.capture_exception(e)
            if db_channel:
                await self.pair()

        # - increase count if param
        await self.send_dm_text(member, translations.resetted_by_team)

    @commands.command(aliases=["s"])
    @guild_only()
    async def statistics(self, ctx: Context):
        """
        show statistics
        """
        if ctx.message.author.bot:
            return
        searching_users: int = await db_thread(
            lambda: db.query(Searcher).filter_by(state=State.QUEUED).count())

        active_donator_list: List[Donator] = await db_thread(
            lambda: db.query(Donator).filter_by(state=State.QUEUED))

        active_donations = sum(u.invite_count - u.used_invites for u in active_donator_list)

        channel_count: int = await db_thread(db.count, Channel)

        completed_searchers: int = await db_thread(
            lambda: db.query(Searcher).filter_by(state=State.DONE).count())

        embed: discord.Embed = discord.Embed(title="Statistiken")
        embed.add_field(name="Suchende User", value=str(searching_users), inline=False)
        embed.add_field(name="Angebotene Einladungen", value=active_donations, inline=False)
        embed.add_field(name="Anzahl der Vermittlungschannels", value=str(channel_count), inline=False)
        embed.add_field(name="Verschenkte Einladungen", value=str(completed_searchers), inline=False)
        await ctx.send(embed=embed)

    @commands.command(aliases=["q"])
    @guild_only()
    async def queue(self, ctx: Context):
        """
        team only
        show queues
        """
        if ctx.message.author.bot:
            return
        if self.team_role not in ctx.author.roles:
            await ctx.send(translations.f_permission_denied(ctx.author.mention))
            return
        searching_users: str = "\n".join(f"<@{user.user_id}>" for user in (await self.calculate_queues())[0])

        active_donator_list: str = "\n".join(
            f"<@{user.user_id}> ({user.invite_count - user.used_invites})"
            for user in (await self.calculate_queues())[1])

        embed: discord.Embed = discord.Embed(title="Warteschlange", color=0x1bcc79)
        embed.add_field(name="Suchende User", value=searching_users or "Keine suchenden User", inline=True)
        embed.add_field(name="Anbietende User", value=active_donator_list or "Keine Angebote", inline=True)
        await send_long_embed(ctx, embed)

    @commands.command()
    @guild_only()
    async def count_queue(self, ctx: Context):
        """
        team only
        get length of direct message queue
        """
        if ctx.message.author.bot:
            return
        if self.team_role not in ctx.author.roles:
            await ctx.send(translations.f_permission_denied(ctx.author.mention))
            return

        await ctx.send(f"Anzahl: {len(self.task_set)}")

    @commands.command(aliases=["us"])
    @guild_only()
    async def unshared_users(self, ctx: Context, ignore_coupled: Optional[bool] = False):
        """
        team only
        get users, which have not shared their invites
        ignore_coupled: if all coupled should be mentioned as well
        """
        if ctx.message.author.bot:
            return
        if self.team_role not in ctx.author.roles:
            await ctx.send(translations.f_permission_denied(ctx.author.mention))
            return

        # 1. alle searcher holen, die DONE sind
        # 2. alle searcher rauswerfen, die einen donator auf DONE haben (beide haben user_id)
        # 3. teilen:
        #   1. alle searcher, die einen channel haben
        #   2. alle searcher, die keinen channel haben

        # s: Set[int] = set(await db_thread(lambda x: x.user_id, db.query(Searcher).filter_by(state=State.DONE).all()))
        s: set[int] = set(
            map(lambda x: x.user_id, await db_thread(lambda: db.query(Searcher).filter_by(state=State.DONE).all())))
        # set(map(lambda y: y.user_id, x))
        # s: Set[Searcher] = set(await db_thread(lambda: db.query(Searcher).filter_by(state=State.DONE).all()))

        t: set[int] = set(
            map(lambda x: x.user_id, await db_thread(lambda: db.query(Donator).filter_by(state=State.DONE).all())))

        missing = s - t

        c: set[int] = set(
            map(lambda x: x.donator_id, await db_thread(lambda: db.query(Channel).all())))

        coupled = missing & c
        not_coupled = missing - c

        out = ""
        for _id in not_coupled:
            out += f"<@{_id}> "
            if len(out) > 1000:
                await ctx.send(out)
                out = ""
        if out:
            await ctx.send(out)

        if not ignore_coupled:
            out = ""
            for _id in coupled:
                out += f"*<@{_id}>\n"
                if len(out) > 1000:
                    await ctx.send(out)
                    out = ""
            if out:
                await ctx.send(out)

            if not coupled and not not_coupled:
                await ctx.send("Keine gefunden!")

        elif not not_coupled:
            await ctx.send("Keine gefunden!")

    @commands.command()
    @guild_only()
    async def reinit_reactions(self, ctx: Context):
        """
        team only
        recreate reactions
        """
        if ctx.message.author.bot:
            return
        if self.team_role not in ctx.author.roles:
            await ctx.send(translations.f_permission_denied(ctx.author.mention))
            return

        await self.start_message.add_reaction(gift)
        await self.start_message.add_reaction(mag)

        await ctx.send(f"DONE")

    @commands.command()
    @guild_only()
    async def requeue(self, ctx: Context):
        """
        team only
        requeue couple of this channel
        """
        if ctx.message.author.bot:
            return
        if self.team_role not in ctx.author.roles:
            await ctx.send(translations.f_permission_denied(ctx.author.mention))
            return
        if ctx.channel.category is None or ctx.channel.category.id not in map(
                lambda x: x.category_id, await db_thread(db.all, Category)):
            await ctx.send(translations.f_wrong_channel(ctx.author.mention))
            return

        db_channel: Optional[Channel] = await db_thread(db.get, Channel, ctx.channel.id)
        if db_channel:
            if await db_thread(db.get, Searcher, db_channel.searcher_id):
                await db_thread(Searcher.change_state, db_channel.searcher_id, State.QUEUED)
            donator: Optional[Donator] = await db_thread(db.get, Donator, db_channel.donator_id)
            if donator:
                await db_thread(Donator.change_used_invites, donator.user_id, max(0, donator.used_invites - 1))
            for db_user in [db_channel.searcher_id, db_channel.donator_id]:
                user: Optional[discord.Member] = self.guild.get_member(db_user)
                if user:
                    await self.send_dm_text(user, translations.back_to_queue)
        try:
            await db_thread(db.delete, db_channel)
            await ctx.channel.delete()
        except (Forbidden, NotFound, HTTPException):
            pass
        await self.pair()

    @commands.command(aliases=["self"])
    async def self_info(self, ctx: Context):
        """
        show own queue status
        """
        if ctx.message.author.bot:
            return

        donator: Optional[Donator] = await db_thread(
            lambda: db.query(Donator)
                .filter_by(user_id=ctx.author.id)
                .filter(Donator.state.in_((State.INITIAL, State.QUEUED, State.MATCHED)))
                .first()
        )
        if donator:
            if donator.state == State.INITIAL:
                await self.send_dm_text(ctx.author, translations.gift_reminder)
            if donator.state == State.QUEUED:
                _, donating_users = await self.calculate_queues()
                index = 0
                for donator2 in donating_users:
                    index += 1
                    if donator2.user_id == donator.user_id:
                        break
                await self.send_dm_text(ctx.author, translations.f_self_queue_status(
                    str(donator.invite_count - donator.used_invites), index))
            if donator.state == State.MATCHED:
                await self.send_dm_text(ctx.author, translations.already_in_room)
            return

        searcher: Optional[Searcher] = await db_thread(
            lambda: db.query(Searcher)
                .filter_by(user_id=ctx.author.id)
                .filter(Searcher.state.in_((State.INITIAL, State.QUEUED, State.MATCHED)))
                .first()
        )
        if searcher:
            if searcher.state == State.INITIAL:
                await self.send_dm_text(ctx.author, translations.read_again)
            if searcher.state == State.QUEUED:
                searching_users, _ = await self.calculate_queues()
                index = 0
                for searcher2 in searching_users:
                    index += 1
                    if searcher2.user_id == searcher.user_id:
                        break
                await self.send_dm_text(ctx.author, translations.f_self_still_in_queue(index))
            if searcher.state == State.MATCHED:
                await self.send_dm_text(ctx.author, translations.already_in_room)
            return

        if not donator and not searcher:
            await self.send_dm_text(ctx.author, translations.self_not_in_queue)
            return

    @commands.command(aliases=["ui"])
    @guild_only()
    async def user_info(self, ctx: Context, member: Optional[Member]):
        """
        show db status of user
        """

        if ctx.message.author.bot:
            return

        if not member:
            await ctx.send(translations.member_not_found)
            return

        if self.team_role not in ctx.author.roles:
            await ctx.send(translations.f_permission_denied(ctx.author.mention))
            return

        searcher: Optional[Searcher] = await db_thread(
            lambda: db.query(Searcher)
                .filter_by(user_id=member.id)
                .first()
        )
        if searcher:
            position = ""
            if searcher.state == State.QUEUED:
                searching_users, _ = await self.calculate_queues()
                index = 0
                for searcher2 in searching_users:
                    index += 1
                    if searcher2.user_id == searcher.user_id:
                        break
                position = f"\nPosition: {index}"
            embed: discord.Embed = discord.Embed(
                title=f"Searcher Status",
                description=f"User: <@{searcher.user_id}>\nState: {searcher.state}{position}"
                            f"\nIn Warteschlange aufgenommen: {searcher.enqueued_at.strftime('%H:%M:%S %d.%m.%Y')}"
            )
            await ctx.send(embed=embed)

        donator: Optional[Donator] = await db_thread(
            lambda: db.query(Donator)
                .filter_by(user_id=member.id)
                .first()
        )
        if donator:
            if donator.state == State.QUEUED:
                _, donating_users = await self.calculate_queues()
                index = 0
                for donator2 in donating_users:
                    index += 1
                    if donator2.user_id == donator.user_id:
                        break
            used_count = donator.invite_count - donator.used_invites
            used = f' (noch {used_count} von {donator.invite_count} Einladungen verfügbar.)'
            description = f"User: <@{donator.user_id}>\nState: {donator.state}"
            description += {
                State.INITIAL: "",
                State.QUEUED: used + f"\nPosition: {index}",
                State.MATCHED: used,
                State.DONE: f" ({donator.invite_count} Einladungen)",
                State.ABORTED: used,
            }[donator.state]

            embed: discord.Embed = discord.Embed(
                title=f"Donator Status",
                description=description
            )
            await ctx.send(embed=embed)

        if not donator and not searcher:
            await ctx.send(translations.f_user_not_found(member.mention))
            return

    @commands.command()
    @guild_only()
    async def rm(self, ctx: Context, member: Optional[Member]):
        """
        team only
        remove a matched user and ban him from the whole process (but not from the server)
        """
        if ctx.message.author.bot:
            return
        if not member:
            await ctx.send(translations.member_not_found)
            return
        if self.team_role not in ctx.author.roles:
            await ctx.send(translations.f_permission_denied(ctx.author.mention))
            return

        user: Union[Donator, Searcher] = await db_thread(
            lambda: db.query(Donator)
                .filter(Donator.user_id == member.id)
                .filter(Donator.state == State.MATCHED)
                .first()
        )
        if not user:
            user = await db_thread(
                lambda: db.query(Searcher)
                    .filter(Searcher.user_id == member.id)
                    .filter(Searcher.state == State.MATCHED)
                    .first()
            )
        if user is None:
            return

        channel: TextChannel = ctx.channel
        if channel.category is None or channel.category.id not in map(lambda x: x.category_id,
                                                                      await db_thread(db.all, Category)):
            await ctx.send(translations.rm_channel)
            return

        db_channel = None
        for db_channel in await db_thread(lambda: db.query(Channel)
                .filter(or_(member.id == Channel.donator_id, member.id == Channel.searcher_id)).all()):

            donator = await db_thread(db.get, Donator, db_channel.donator_id)
            other_id = 0
            if donator:
                if member.id == donator.user_id:
                    await db_thread(Donator.change_state, member.id, State.ABORTED)
                else:
                    await db_thread(Donator.change_used_invites, donator.user_id,
                                    max(0, donator.used_invites - 1))
                    other_id = donator.user_id

            searcher = await db_thread(db.get, Searcher, db_channel.searcher_id)
            if searcher:
                if member.id == searcher.user_id:
                    await db_thread(Searcher.change_state, searcher.user_id, State.ABORTED)
                else:
                    await db_thread(Searcher.change_state, searcher.user_id, State.QUEUED)
                    other_id = searcher.user_id

            if other_id != 0 and (other_user := self.bot.get_user(other_id)) is not None:
                await self.send_dm_text(other_user, translations.chanel_was_closed_by_team)

            channel: Optional[TextChannel] = self.bot.get_channel(db_channel.channel_id)
            await db_thread(db.delete, db_channel)
            try:
                if channel:
                    await channel.delete()
            except Exception as e:
                sentry_sdk.capture_exception(e)
        if db_channel:
            await self.pair()
        await self.send_dm_text(member, translations.chanel_was_closed_by_team)

    if getenv("DEBUG") == "true" or getenv("DEBUG") == 1:
        @commands.command(aliases=["del"])
        @guild_only()
        async def delete(self, ctx: Context):
            """
            clear user tables
            """
            if ctx.message.author.bot:
                return
            session = db.session
            session.query(Searcher).delete()
            session.query(Channel).delete()
            session.query(Donator).delete()
            session.commit()
            await ctx.send("Done")

        @commands.command()
        @guild_only()
        async def kick(self, ctx: Context, member: Member):
            """
            kicks a user (well the bot thinks that)
            """
            if ctx.message.author.bot:
                return
            await self.on_member_remove(member)
            await ctx.send("Done")
