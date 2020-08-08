import asyncio
import functools
import logging
import re
import sys
from collections import OrderedDict, defaultdict
from contextlib import suppress
from types import SimpleNamespace
from typing import Dict, NamedTuple, Optional, List

import discord
from bs4 import BeautifulSoup
from discord.ext import commands
from requests import ConnectTimeout, ConnectionError, HTTPError
from sphinx.ext import intersphinx
from urllib3.exceptions import ProtocolError

from bot.bot import Bot
from bot.constants import MODERATION_ROLES, RedirectOutput
from bot.converters import PackageName, ValidURL
from bot.decorators import with_role
from bot.pagination import LinePaginator
from bot.utils.messages import wait_for_deletion
from .cache import async_cache
from .parsing import get_symbol_markdown

log = logging.getLogger(__name__)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Since Intersphinx is intended to be used with Sphinx,
# we need to mock its configuration.
SPHINX_MOCK_APP = SimpleNamespace(
    config=SimpleNamespace(
        intersphinx_timeout=3,
        tls_verify=True,
        user_agent="python3:python-discord/bot:1.0.0"
    )
)

NO_OVERRIDE_GROUPS = (
    "2to3fixer",
    "token",
    "label",
    "pdbcommand",
    "term",
)
NO_OVERRIDE_PACKAGES = (
    "python",
)

WHITESPACE_AFTER_NEWLINES_RE = re.compile(r"(?<=\n\n)(\s+)")
FAILED_REQUEST_RETRY_AMOUNT = 3
NOT_FOUND_DELETE_DELAY = RedirectOutput.delete_delay


class TODO_PLACEHOLDER:
    __slots__ = ("queue", "item_events", "started", "fetch_lock")
    results_dict: Dict[str, discord.Embed] = None

    def __init__(self):
        self.queue: List[DocItem] = list()
        self.fetch_lock = asyncio.Lock()
        self.started = False
        self.item_events: Dict[DocItem, asyncio.Event] = {}

    @classmethod
    def set_results_dict(cls, results_dict: Dict[str, discord.Embed]):
        """
        Set dict for parse results for all instances.

        Must be set before any parsing is attempted.
        """
        cls.results_dict = results_dict

    def add_item(self, item):
        """Add `item` into the parse queue."""
        self.queue.append(item)

    def put_to_front(self, item):
        """Move `item` to the front of the parse queue."""
        self.queue.remove(item)
        self.queue.append(item)

    async def wait_for_item(self, symbol_info, soup: Optional[BeautifulSoup] = None):
        """
        Block until `symbol_info` is parsed and assigned to `cls.results_dict`.

        When not started, `soup` must be provided to start the parsing task;
        after the initial request, providing the soup is unnecessary.
        """
        self.put_to_front(symbol_info)
        item_event = asyncio.Event()
        # First request
        if not self.item_events:
            log.info(f"Started {symbol_info.base_url + symbol_info.relative_url_path}")
            self.item_events[symbol_info] = item_event
            asyncio.create_task(self.parse_items(soup))
        else:
            self.item_events[symbol_info] = item_event
        await item_event.wait()

    async def parse_items(self, soup):
        """Parse all item from `soup` and assign their result embeds into `cls.results_dict`."""
        while self.queue:
            item = self.queue.pop()
            embed = discord.Embed(
                title=discord.utils.escape_markdown(item.name),
                url=item.url,
                description=get_symbol_markdown(soup, item)
            )
            self.results_dict[item.name] = embed
            event = self.item_events.get(item)
            if event is not None:
                event.set()
            await asyncio.sleep(0.1)
        log.info(f"finished {item.base_url + item.relative_url_path}")


class DocItem(NamedTuple):
    """Holds inventory symbol information."""

    name: str
    package: str
    group: str
    base_url: str
    relative_url_path: str
    symbol_id: str

    @property
    def url(self) -> str:
        """Return the absolute url to the symbol."""
        return "".join((self.base_url, self.relative_url_path, "#", self.symbol_id))


class InventoryURL(commands.Converter):
    """
    Represents an Intersphinx inventory URL.

    This converter checks whether intersphinx accepts the given inventory URL, and raises
    `BadArgument` if that is not the case.

    Otherwise, it simply passes through the given URL.
    """

    @staticmethod
    async def convert(ctx: commands.Context, url: str) -> str:
        """Convert url to Intersphinx inventory URL."""
        await ctx.trigger_typing()
        try:
            intersphinx.fetch_inventory(SPHINX_MOCK_APP, '', url)
        except AttributeError:
            raise commands.BadArgument(f"Failed to fetch Intersphinx inventory from URL `{url}`.")
        except ConnectionError:
            if url.startswith('https'):
                raise commands.BadArgument(
                    f"Cannot establish a connection to `{url}`. Does it support HTTPS?"
                )
            raise commands.BadArgument(f"Cannot connect to host with URL `{url}`.")
        except ValueError:
            raise commands.BadArgument(
                f"Failed to read Intersphinx inventory from URL `{url}`. "
                "Are you sure that it's a valid inventory file?"
            )
        return url


class DocCog(commands.Cog):
    """A set of commands for querying & displaying documentation."""

    def __init__(self, bot: Bot):
        self.base_urls = {}
        self.bot = bot
        self.doc_symbols: Dict[str, DocItem] = {}
        self.renamed_symbols = set()
        self.urls: Dict[str, TODO_PLACEHOLDER] = defaultdict(TODO_PLACEHOLDER)
        self.objects = {}
        self.bot.loop.create_task(self.init_refresh_inventory())
        TODO_PLACEHOLDER.set_results_dict(self.objects)
        # TODO decide on objects/doc_symbols merging and what needs to be kept in doc_symbols when urls exist

    async def init_refresh_inventory(self) -> None:
        """Refresh documentation inventory on cog initialization."""
        await self.bot.wait_until_guild_available()
        await self.refresh_inventory()

    async def update_single(
        self, api_package_name: str, base_url: str, inventory_url: str
    ) -> None:
        """
        Rebuild the inventory for a single package.

        Where:
            * `package_name` is the package name to use, appears in the log
            * `base_url` is the root documentation URL for the specified package, used to build
                absolute paths that link to specific symbols
            * `inventory_url` is the absolute URL to the intersphinx inventory, fetched by running
                `intersphinx.fetch_inventory` in an executor on the bot's event loop
        """
        self.base_urls[api_package_name] = base_url

        package = await self._fetch_inventory(inventory_url)
        if not package:
            return None

        for group, value in package.items():
            for symbol, (_package_name, _version, relative_doc_url, _) in value.items():
                if "/" in symbol:
                    continue  # skip unreachable symbols with slashes
                # Intern the group names since they're reused in all the DocItems
                # to remove unnecessary memory consumption from them being unique objects
                group_name = sys.intern(group.split(":")[1])

                if symbol in self.doc_symbols:
                    symbol_base_url = self.doc_symbols[symbol].url.split("/", 3)[2]
                    if (
                        group_name in NO_OVERRIDE_GROUPS
                        or any(package in symbol_base_url for package in NO_OVERRIDE_PACKAGES)
                    ):
                        symbol = f"{group_name}.{symbol}"
                        self.renamed_symbols.add(symbol)

                    elif (overridden_symbol_group := self.doc_symbols[symbol].group) in NO_OVERRIDE_GROUPS:
                        overridden_symbol = f"{overridden_symbol_group}.{symbol}"
                        if overridden_symbol in self.renamed_symbols:
                            overridden_symbol = f"{api_package_name}.{overridden_symbol}"

                        self.doc_symbols[overridden_symbol] = self.doc_symbols[symbol]
                        self.renamed_symbols.add(overridden_symbol)

                    # If renamed `symbol` already exists, add library name in front to differentiate between them.
                    elif symbol in self.renamed_symbols:
                        # Split `package_name` because of packages like Pillow that have spaces in them.
                        symbol = f"{api_package_name}.{symbol}"
                        self.renamed_symbols.add(symbol)
                # TODO remove comment above, clean up
                relative_url_path, _, symbol_id = relative_doc_url.partition("#")
                self.doc_symbols[symbol] = DocItem(symbol, api_package_name,group_name, base_url, relative_url_path, symbol_id)
                self.urls[base_url+relative_url_path].add_item(self.doc_symbols[symbol])

        log.trace(f"Fetched inventory for {api_package_name}.")

    async def refresh_inventory(self) -> None:
        """Refresh internal documentation inventory."""
        log.debug("Refreshing documentation inventory...")

        # Clear the old base URLS and doc symbols to ensure
        # that we start from a fresh local dataset.
        # Also, reset the cache used for fetching documentation.
        self.base_urls.clear()
        self.doc_symbols.clear()
        self.renamed_symbols.clear()
        async_cache.cache = OrderedDict()

        # Run all coroutines concurrently - since each of them performs a HTTP
        # request, this speeds up fetching the inventory data heavily.
        coros = [
            self.update_single(
                package["package"], package["base_url"], package["inventory_url"]
            ) for package in await self.bot.api_client.get('bot/documentation-links')
        ]
        await asyncio.gather(*coros)

    async def get_symbol_markdown(self, symbol_data: DocItem):
        """TODO"""
        if symbol_data.name not in self.objects:
            q = self.urls[symbol_data.base_url + symbol_data.relative_url_path]
            if not q.started:
                async with self.bot.http_session.get(symbol_data.url) as response:
                    soup = BeautifulSoup(await response.text(encoding="utf8"), 'lxml')
                    soup.find("head").decompose()
            else:
                soup = None
            await q.wait_for_item(symbol_data, soup)
        return self.objects[symbol_data.name]

    async def get_symbol_embed(self, symbol: str) -> Optional[discord.Embed]:
        """
        Attempt to scrape and fetch the data for the given `symbol`, and build an embed from its contents.

        If the symbol is known, an Embed with documentation about it is returned.
        """
        symbol_info = self.doc_symbols.get(symbol)
        if symbol_info is None:
            return None

        self.bot.stats.incr(f"doc_fetches.{symbol_info.package.lower()}")
        embed = await self.get_symbol_markdown(symbol_info)
        # Show all symbols with the same name that were renamed in the footer.
        embed.set_footer(
            text=", ".join(renamed for renamed in self.renamed_symbols - {symbol} if renamed.endswith(f".{symbol}"))
        )
        return embed

    @commands.group(name='docs', aliases=('doc', 'd'), invoke_without_command=True)
    async def docs_group(self, ctx: commands.Context, *, symbol: Optional[str]) -> None:
        """Lookup documentation for Python symbols."""
        await ctx.invoke(self.get_command, symbol=symbol)

    @commands.command()
    async def command(self, ctx):
        for symbol in ["arcade", "arcade"]:
                               await self.get_command(ctx, symbol=symbol)

    @docs_group.command(name='getdoc', aliases=('g',))
    async def get_command(self, ctx: commands.Context, *, symbol: Optional[str]) -> None:
        """
        Return a documentation embed for a given symbol.

        If no symbol is given, return a list of all available inventories.

        Examples:
            !docs
            !docs aiohttp
            !docs aiohttp.ClientSession
            !docs getdoc aiohttp.ClientSession
        """
        if not symbol:
            inventory_embed = discord.Embed(
                title=f"All inventories (`{len(self.base_urls)}` total)",
                colour=discord.Colour.blue()
            )

            lines = sorted(f"• [`{name}`]({url})" for name, url in self.base_urls.items())
            if self.base_urls:
                await LinePaginator.paginate(lines, ctx, inventory_embed, max_size=400, empty=False)

            else:
                inventory_embed.description = "Hmmm, seems like there's nothing here yet."
                await ctx.send(embed=inventory_embed)

        else:
            symbol = symbol.strip("`")
            # Fetching documentation for a symbol (at least for the first time, since
            # caching is used) takes quite some time, so let's send typing to indicate
            # that we got the command, but are still working on it.
            async with ctx.typing():
                doc_embed = await self.get_symbol_embed(symbol)

            if doc_embed is None:
                symbol = await discord.ext.commands.clean_content().convert(ctx, symbol)
                error_embed = discord.Embed(
                    description=f"Sorry, I could not find any documentation for `{(symbol)}`.",
                    colour=discord.Colour.red()
                )
                error_message = await ctx.send(embed=error_embed)
                await wait_for_deletion(
                    error_message,
                    (ctx.author.id,),
                    timeout=NOT_FOUND_DELETE_DELAY,
                    client=self.bot
                )
                with suppress(discord.NotFound):
                    await ctx.message.delete()
                with suppress(discord.NotFound):
                    await error_message.delete()
            else:
                await ctx.send(embed=doc_embed)

    @docs_group.command(name='setdoc', aliases=('s',))
    @with_role(*MODERATION_ROLES)
    async def set_command(
        self, ctx: commands.Context, package_name: PackageName,
        base_url: ValidURL, inventory_url: InventoryURL
    ) -> None:
        """
        Adds a new documentation metadata object to the site's database.

        The database will update the object, should an existing item with the specified `package_name` already exist.

        Example:
            !docs setdoc \
                    python \
                    https://docs.python.org/3/ \
                    https://docs.python.org/3/objects.inv
        """
        body = {
            'package': package_name,
            'base_url': base_url,
            'inventory_url': inventory_url
        }
        await self.bot.api_client.post('bot/documentation-links', json=body)

        log.info(
            f"User @{ctx.author} ({ctx.author.id}) added a new documentation package:\n"
            f"Package name: {package_name}\n"
            f"Base url: {base_url}\n"
            f"Inventory URL: {inventory_url}"
        )

        await self.update_single(package_name, base_url, inventory_url)
        await ctx.send(f"Added package `{package_name}` to database and refreshed inventory.")

    @docs_group.command(name='deletedoc', aliases=('removedoc', 'rm', 'd'))
    @with_role(*MODERATION_ROLES)
    async def delete_command(self, ctx: commands.Context, package_name: PackageName) -> None:
        """
        Removes the specified package from the database.

        Examples:
            !docs deletedoc aiohttp
        """
        await self.bot.api_client.delete(f'bot/documentation-links/{package_name}')

        async with ctx.typing():
            # Rebuild the inventory to ensure that everything
            # that was from this package is properly deleted.
            await self.refresh_inventory()
        await ctx.send(f"Successfully deleted `{package_name}` and refreshed inventory.")

    @docs_group.command(name="refreshdoc", aliases=("rfsh", "r"))
    @with_role(*MODERATION_ROLES)
    async def refresh_command(self, ctx: commands.Context) -> None:
        """Refresh inventories and send differences to channel."""
        old_inventories = set(self.base_urls)
        with ctx.typing():
            await self.refresh_inventory()
        new_inventories = set(self.base_urls)

        if added := ", ".join(new_inventories - old_inventories):
            added = "+ " + added

        if removed := ", ".join(old_inventories - new_inventories):
            removed = "- " + removed

        embed = discord.Embed(
            title="Inventories refreshed",
            description=f"```diff\n{added}\n{removed}```" if added or removed else ""
        )
        await ctx.send(embed=embed)

    async def _fetch_inventory(self, inventory_url: str) -> Optional[dict]:
        """Get and return inventory from `inventory_url`. If fetching fails, return None."""
        fetch_func = functools.partial(intersphinx.fetch_inventory, SPHINX_MOCK_APP, '', inventory_url)
        for retry in range(1, FAILED_REQUEST_RETRY_AMOUNT+1):
            try:
                package = await self.bot.loop.run_in_executor(None, fetch_func)
            except ConnectTimeout:
                log.error(
                    f"Fetching of inventory {inventory_url} timed out,"
                    f" trying again. ({retry}/{FAILED_REQUEST_RETRY_AMOUNT})"
                )
            except ProtocolError:
                log.error(
                    f"Connection lost while fetching inventory {inventory_url},"
                    f" trying again. ({retry}/{FAILED_REQUEST_RETRY_AMOUNT})"
                )
            except HTTPError as e:
                log.error(f"Fetching of inventory {inventory_url} failed with status code {e.response.status_code}.")
                return None
            except ConnectionError:
                log.error(f"Couldn't establish connection to inventory {inventory_url}.")
                return None
            else:
                return package
        log.error(f"Fetching of inventory {inventory_url} failed.")
        return None
