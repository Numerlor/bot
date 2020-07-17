import asyncio
import functools
import logging
import re
import sys
import textwrap
from collections import OrderedDict
from contextlib import suppress
from types import SimpleNamespace
import typing as t
from urllib.parse import urljoin

import discord
from bs4 import BeautifulSoup
from bs4.element import PageElement, Tag, SoupStrainer
from discord.ext import commands
from markdownify import MarkdownConverter
from requests import ConnectTimeout, ConnectionError, HTTPError
from sphinx.ext import intersphinx
from urllib3.exceptions import ProtocolError

from bot.bot import Bot
from bot.constants import MODERATION_ROLES, RedirectOutput
from bot.converters import PackageName, ValidURL
from bot.decorators import with_role
from bot.pagination import LinePaginator
from bot.utils.messages import wait_for_deletion


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

SEARCH_END_TAG_ATTRS = (
    "data",
    "function",
    "class",
    "exception",
    "seealso",
    "section",
    "rubric",
    "sphinxsidebar",
)
UNWANTED_SIGNATURE_SYMBOLS_RE = re.compile(r"\[source]|\\\\|¶")
WHITESPACE_AFTER_NEWLINES_RE = re.compile(r"(?<=\n\n)(\s+)")

FAILED_REQUEST_RETRY_AMOUNT = 3
NOT_FOUND_DELETE_DELAY = RedirectOutput.delete_delay


class BS4FindManyMethod(t.Protocol):
    bs4_filter = t.Union[str, t.Pattern, t.List["bs4_filter"], t.Literal[True]]
    """Represent a find method from bs4 such as `find_all_next` that returns multiple `Tag`s."""
    def __call__(
            self,
            instance: PageElement,
            name: t.Optional[t.Union[bs4_filter, SoupStrainer]] = None,
            attrs: t.Optional[t.Dict[str, t.Any]] = None,
            text: t.Optional[bs4_filter] = None,
            limit: t.Optional[int] = None,
            **kwargs: bs4_filter,
    ) -> t.Iterable[Tag]:
        ...


class DocItem(t.NamedTuple):
    """Holds inventory symbol information."""

    package: str
    url: str
    group: str


def async_cache(max_size: int = 128, arg_offset: int = 0) -> t.Callable:
    """
    LRU cache implementation for coroutines.

    Once the cache exceeds the maximum size, keys are deleted in FIFO order.

    An offset may be optionally provided to be applied to the coroutine's arguments when creating the cache key.
    """
    # Assign the cache to the function itself so we can clear it from outside.
    async_cache.cache = OrderedDict()

    def decorator(function: t.Callable) -> t.Callable:
        """Define the async_cache decorator."""
        @functools.wraps(function)
        async def wrapper(*args) -> t.Any:
            """Decorator wrapper for the caching logic."""
            key = ':'.join(args[arg_offset:])

            value = async_cache.cache.get(key)
            if value is None:
                if len(async_cache.cache) > max_size:
                    async_cache.cache.popitem(last=False)

                async_cache.cache[key] = await function(*args)
            return async_cache.cache[key]
        return wrapper
    return decorator


class DocMarkdownConverter(MarkdownConverter):
    """Subclass markdownify's MarkdownCoverter to provide custom conversion methods."""

    def __init__(self, *, page_url: str, **options):
        super().__init__(**options)
        self.page_url = page_url

    def convert_code(self, el: PageElement, text: str) -> str:
        """Undo `markdownify`s underscore escaping."""
        return f"`{text}`".replace('\\', '')

    def convert_pre(self, el: PageElement, text: str) -> str:
        """Wrap any codeblocks in `py` for syntax highlighting."""
        code = ''.join(el.strings)
        return f"```py\n{code}```"

    def convert_a(self, el: PageElement, text: str) -> str:
        """Resolve relative URLs to `self.page_url`."""
        el["href"] = urljoin(self.page_url, el["href"])
        return super().convert_a(el, text)

    def convert_p(self, el: PageElement, text: str) -> str:
        """Include only one newline instead of two when the parent is a li tag."""
        parent = el.parent
        if parent is not None and parent.name == "li":
            return f"{text}\n"
        return super().convert_p(el, text)


def markdownify(html: str, *, url: str = "") -> str:
    """Create a DocMarkdownConverter object from the input html."""
    return DocMarkdownConverter(bullets='•', page_url=url).convert(html)


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


class Doc(commands.Cog):
    """A set of commands for querying & displaying documentation."""

    def __init__(self, bot: Bot):
        self.base_urls = {}
        self.bot = bot
        self.doc_symbols: t.Dict[str, DocItem] = {}
        self.renamed_symbols = set()

        self.bot.loop.create_task(self.init_refresh_inventory())

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
                absolute_doc_url = base_url + relative_doc_url
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

                    elif (overridden_symbol_group := self.doc_symbols[symbol].group) in NO_OVERRIDE_GROUPS:
                        overridden_symbol = f"{overridden_symbol_group}.{symbol}"
                        if overridden_symbol in self.renamed_symbols:
                            overridden_symbol = f"{api_package_name}.{overridden_symbol}"

                        self.doc_symbols[overridden_symbol] = self.doc_symbols[symbol]
                        self.renamed_symbols.add(overridden_symbol)

                    # If renamed `symbol` already exists, add library name in front to differentiate between them.
                    if symbol in self.renamed_symbols:
                        # Split `package_name` because of packages like Pillow that have spaces in them.
                        symbol = f"{api_package_name}.{symbol}"
                        self.renamed_symbols.add(symbol)

                self.doc_symbols[symbol] = DocItem(api_package_name, absolute_doc_url, group_name)

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

    async def get_symbol_html(self, symbol: str) -> t.Optional[t.Tuple[list, str]]:
        """
        Given a Python symbol, return its signature and description.

        The first tuple element is the signature of the given symbol as a markup-free string, and
        the second tuple element is the description of the given symbol with HTML markup included.

        If the given symbol is a module, returns a tuple `(None, str)`
        else if the symbol could not be found, returns `None`.
        """
        symbol_info = self.doc_symbols.get(symbol)
        if symbol_info is None:
            return None
        request_url, symbol_id = symbol_info.url.rsplit('#')

        soup = await self._get_soup_from_url(request_url)
        symbol_heading = soup.find(id=symbol_id)
        search_html = str(soup)

        if symbol_heading is None:
            return None

        if symbol_info.group == "module":
            parsed_module = self.parse_module_symbol(symbol_heading)
            if parsed_module is None:
                return [], ""
            else:
                signatures, description = parsed_module

        else:
            signatures = self.get_signatures(symbol_heading)
            description = "".join(map(str, self.find_next_children_until_tag(symbol_heading.find_next("dd"), ("dt", "dl"))))

        return signatures, description.replace('¶', '')

    @async_cache(arg_offset=1)
    async def get_symbol_embed(self, symbol: str) -> t.Optional[discord.Embed]:
        """
        Attempt to scrape and fetch the data for the given `symbol`, and build an embed from its contents.

        If the symbol is known, an Embed with documentation about it is returned.
        """
        scraped_html = await self.get_symbol_html(symbol)
        if scraped_html is None:
            return None

        symbol_obj = self.doc_symbols[symbol]
        self.bot.stats.incr(f"doc_fetches.{symbol_obj.package.lower()}")
        signatures, html = scraped_html
        permalink = symbol_obj.url
        description = self.truncate_markdown(markdownify(html, url=permalink), max_length=1000, max_lines=15)

        description = WHITESPACE_AFTER_NEWLINES_RE.sub('', description)
        if signatures is None:
            # If symbol is a module, don't show signature.
            embed_description = description

        elif not signatures:
            # It's some "meta-page", for example:
            # https://docs.djangoproject.com/en/dev/ref/views/#module-django.views
            embed_description = "This appears to be a generic page not tied to a specific symbol."

        else:
            embed_description = "".join(f"```py\n{textwrap.shorten(signature, 500)}```" for signature in signatures)
            embed_description += f"\n{description}"

        embed = discord.Embed(
            title=discord.utils.escape_markdown(symbol),
            url=permalink,
            description=embed_description
        )
        # Show all symbols with the same name that were renamed in the footer.
        embed.set_footer(
            text=", ".join(renamed for renamed in self.renamed_symbols - {symbol} if renamed.endswith(f".{symbol}"))
        )
        return embed

    @classmethod
    def get_description(cls, heading: PageElement) -> t.Optional[t.Tuple[None, str]]:
        """Get page content from the headerlink up to a table or a tag with its class in `SEARCH_END_TAG_ATTRS`."""
        start_tag = heading.find("a", attrs={"class": "headerlink"})
        if start_tag is None:
            return None

        description = cls.find_next_children_until_tag(start_tag, cls._match_end_tag)
        if description is None:
            return None

        return None, description

    @classmethod
    def get_signatures(cls, start_signature: PageElement) -> t.List[str]:
        """Collect up to 3 signatures from dt tags around the `start_signature` dt tag."""
        signatures = []
        for element in (
            *reversed(cls.find_previous_siblings_until_tag(start_signature, ("dd",), limit=2)),
            start_signature,
            *cls.find_next_siblings_until_tag(start_signature, ("dd",), limit=2),
        )[-3:]:
            signature = UNWANTED_SIGNATURE_SYMBOLS_RE.sub("", element.text if type(element) is not str else element)

            if signature:
                signatures.append(signature)

        return signatures

    def find_elements_until_tag(
            start_element: PageElement,
            tag_filter: t.Union[t.Tuple[str, ...], t.Callable[[Tag], bool]],
            *,
            func: BS4FindManyMethod,
            limit: int = None,
    ) -> t.Optional[str]:
        """
        Get all tags from a bs4 find many method until a tag matching `tag_filter` is found.

        `tag_filter` can be either a tuple of string names to check against,
        or a filtering t.Callable that's applied to the tags.
        """
        elements = []
        for element in func(start_element, limit=limit):
            if isinstance(tag_filter, tuple):
                if element.name in tag_filter:
                    break
            elif tag_filter(element):
                break
            elements.append(element)

        return elements

    find_next_children_until_tag = staticmethod(functools.partial(find_elements_until_tag, func=functools.partial(BeautifulSoup.find_all, recursive=False)))
    find_previous_children_until_tag = staticmethod(functools.partial(find_elements_until_tag, func=BeautifulSoup.find_all_previous))
    find_next_siblings_until_tag = staticmethod(functools.partial(find_elements_until_tag, func=BeautifulSoup.find_next_siblings))
    find_previous_siblings_until_tag = staticmethod(functools.partial(find_elements_until_tag, func=BeautifulSoup.find_previous_siblings))
    find_elements_until_tag = staticmethod(find_elements_until_tag)

    @async_cache(arg_offset=1)
    async def _get_soup_from_url(self, url: str) -> BeautifulSoup:
        """Create a BeautifulSoup object from the HTML data in `url` with the head tag removed."""
        log.trace(f"Sending a request to {url}.")
        async with self.bot.http_session.get(url) as response:
            soup = BeautifulSoup(await response.text(encoding="utf8"), 'lxml')
        soup.find("head").decompose()  # the head contains no useful data so we can remove it
        return soup

    @commands.group(name='docs', aliases=('doc', 'd'), invoke_without_command=True)
    async def docs_group(self, ctx: commands.Context, *, symbol: t.Optional[str]) -> None:
        """Lookup documentation for Python symbols."""
        await ctx.invoke(self.get_command, symbol=symbol)

    @docs_group.command(name='getdoc', aliases=('g',))
    async def get_command(self, ctx: commands.Context, *, symbol: t.Optional[str]) -> None:
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

    async def _fetch_inventory(self, inventory_url: str) -> t.Optional[dict]:
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

    @staticmethod
    def _match_end_tag(tag: Tag) -> bool:
        """Matches `tag` if its class value is in `SEARCH_END_TAG_ATTRS` or the tag is table."""
        for attr in SEARCH_END_TAG_ATTRS:
            if attr in tag.get("class", ()):
                return True

        return tag.name == "table"


def setup(bot: Bot) -> None:
    """Load the Doc cog."""
    bot.add_cog(Doc(bot))
