import logging
import re
import string
import textwrap
from functools import partial
from typing import Callable, List, Optional, TYPE_CHECKING, Tuple, Union

from aiohttp import ClientSession
from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

from .cache import async_cache
from .html import Strainer
from .markdown import markdownify
if TYPE_CHECKING:
    from .cog import DocItem

log = logging.getLogger(__name__)

_UNWANTED_SIGNATURE_SYMBOLS_RE = re.compile(r"\[source]|\\\\|¶")
_WHITESPACE_AFTER_NEWLINES_RE = re.compile(r"(?<=\n\n)(\s+)")

_SEARCH_END_TAG_ATTRS = (
    "data",
    "function",
    "class",
    "exception",
    "seealso",
    "section",
    "rubric",
    "sphinxsidebar",
)

_NO_SIGNATURE_GROUPS = {
    "attribute",
    "envvar",
    "setting",
    "tempaltefilter",
    "templatetag",
    "term",
}


def _find_elements_until_tag(
        start_element: PageElement,
        tag_filter: Union[Tuple[str, ...], Callable[[Tag], bool]],
        *,
        func: Callable,
        include_strings: bool = False,
        limit: int = None,
) -> List[Union[Tag, NavigableString]]:
    """
    Get all elements up to `limit` or until a tag matching `tag_filter` is found.

    `tag_filter` can be either a tuple of string names to check against,
    or a filtering callable that's applied to tags.

    When `include_strings` is True, `NavigableString`s from the document will be included in the result along `Tag`s.

    `func` takes in a BeautifulSoup unbound method for finding multiple elements, such as `BeautifulSoup.find_all`.
    The method is then iterated over and all elements until the matching tag or the limit are added to the return list.
    """
    use_tuple_filter = isinstance(tag_filter, tuple)
    elements = []

    for element in func(start_element, name=Strainer(include_strings=include_strings), limit=limit):
        if isinstance(element, Tag):
            if use_tuple_filter:
                if element.name in tag_filter:
                    break
            elif tag_filter(element):
                break
        elements.append(element)

    return elements


_find_next_children_until_tag = partial(_find_elements_until_tag, func=partial(BeautifulSoup.find_all, recursive=False))
_find_next_siblings_until_tag = partial(_find_elements_until_tag, func=BeautifulSoup.find_next_siblings)
_find_previous_siblings_until_tag = partial(_find_elements_until_tag, func=BeautifulSoup.find_previous_siblings)


def _get_general_description(start_element: PageElement) -> Optional[str]:
    """
    Get page content to a table or a tag with its class in `SEARCH_END_TAG_ATTRS`.

    A headerlink a tag is attempted to be found to skip repeating the symbol information in the description,
    if it's found it's used as the tag to start the search from instead of the `start_element`.
    """
    header = start_element.find_next("a", attrs={"class": "headerlink"})
    start_tag = header.parent if header is not None else start_element
    result_tags = _find_next_siblings_until_tag(start_tag, _match_end_tag, include_strings=True)
    description = "".join(str(tag) for tag in result_tags)


    return description


def _get_dd_description(symbol: PageElement) -> str:
    """Get the string contents of the next dd tag, up to a dt or a dl tag."""
    description_tag = symbol.find_next("dd")
    description_contents = _find_next_children_until_tag(description_tag, ("dt", "dl"), include_strings=True)
    description = "".join(str(tag) for tag in description_contents)

    return description


def _get_signatures(start_signature: PageElement) -> List[str]:
    """
    Collect up to 3 signatures from dt tags around the `start_signature` dt tag.

    First the signatures under the `start_signature` are included;
    if less than 2 are found, tags above the start signature are added to the result if any are present.
    """
    signatures = []
    for element in (
            *reversed(_find_previous_siblings_until_tag(start_signature, ("dd",), limit=2)),
            start_signature,
            *_find_next_siblings_until_tag(start_signature, ("dd",), limit=2),
    )[-3:]:
        signature = _UNWANTED_SIGNATURE_SYMBOLS_RE.sub("", element.text)

        if signature:
            signatures.append(signature)

    return signatures


def _truncate_markdown(markdown: str, max_length: int) -> str:
    """
    Truncate `markdown` to be at most `max_length` characters.

    The markdown string is searched for substrings to cut at, to keep its structure,
    but if none are found the string is simply sliced.
    """
    if len(markdown) > max_length:
        shortened = markdown[:max_length]
        description_cutoff = shortened.rfind('\n\n', 100)
        if description_cutoff == -1:
            # Search the shortened version for cutoff points in decreasing desirability,
            # cutoff at 1000 if none are found.
            for cutoff_string in (". ", ", ", ",", " "):
                description_cutoff = shortened.rfind(cutoff_string)
                if description_cutoff != -1:
                    break
            else:
                description_cutoff = max_length
        markdown = markdown[:description_cutoff]

        # If there is an incomplete code block, cut it out
        if markdown.count("```") % 2:
            codeblock_start = markdown.rfind('```py')
            markdown = markdown[:codeblock_start].rstrip()
        markdown = markdown.rstrip(string.punctuation) + "..."
    return markdown


def _parse_into_markdown(signatures: Optional[List[str]], description: str, url: str) -> str:
    """
    Create a markdown string with the signatures at the top, and the converted html description below them.

    The signatures are wrapped in python codeblocks, separated from the description by a newline.
    The result string is truncated to be max 1000 symbols long.
    """
    description = _truncate_markdown(markdownify(description, url=url), 1000)
    description = _WHITESPACE_AFTER_NEWLINES_RE.sub('', description)
    if signatures is not None:
        formatted_markdown = "".join(f"```py\n{textwrap.shorten(signature, 500)}```" for signature in signatures)
    else:
        formatted_markdown = ""
    formatted_markdown += f"\n{description}"

    return formatted_markdown


@async_cache(arg_offset=1)
async def _get_soup_from_url(http_session: ClientSession, url: str) -> BeautifulSoup:
    """Create a BeautifulSoup object from the HTML data in `url` with the head tag removed."""
    log.trace(f"Sending a request to {url}.")
    async with http_session.get(url) as response:
        soup = BeautifulSoup(await response.text(encoding="utf8"), 'lxml')
    soup.find("head").decompose()  # the head contains no useful data so we can remove it
    return soup


def _match_end_tag(tag: Tag) -> bool:
    """Matches `tag` if its class value is in `SEARCH_END_TAG_ATTRS` or the tag is table."""
    for attr in _SEARCH_END_TAG_ATTRS:
        if attr in tag.get("class", ()):
            return True

    return tag.name == "table"


def get_symbol_markdown(soup: BeautifulSoup, symbol_data: "DocItem") -> str:
    """
    Return parsed markdown of the passed symbol, truncated to 1000 characters.

    A request through `http_session` is made to the url associated with `symbol_data` for the html contents;
    the contents are then parsed depending on what group the symbol belongs to.
    """
    # log.trace(f"Parsing symbol from url {symbol_data.url}.")
    symbol_heading = soup.find(id=symbol_data.symbol_id)
    signature = None
    # Modules, doc pages and labels don't point to description list tags but to tags like divs,
    # no special parsing can be done so we only try to include what's under them.
    if symbol_data.group in {"module", "doc", "label"}:
        # log.trace("Symbol is a module, doc or a label; using general description parsing.")
        description = _get_general_description(symbol_heading)

    elif symbol_heading.name != "dt":
        # Use the general parsing for symbols that aren't modules, docs or labels and aren't dt tags,
        # log info the tag can be looked at.
        # log.info(
        #     f"Symbol heading at url {symbol_data.url} was not a dt tag or from known groups that lack it,"
        #     f"handling as general description."
        # )
        description = _get_general_description(symbol_heading)

    elif symbol_data.group in _NO_SIGNATURE_GROUPS:
        # log.trace("Symbol's group is in the group signature blacklist, skipping parsing of signature.")
        description = _get_dd_description(symbol_heading)

    else:
        # log.trace("Parsing both signature and description of symbol.")
        description = _get_dd_description(symbol_heading)
        signature = _get_signatures(symbol_heading)

    return _parse_into_markdown(signature, description.replace('¶', ''), symbol_data.url)
