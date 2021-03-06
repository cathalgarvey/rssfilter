#!/usr/bin/env python3
"""An RSS filtering tool/library that helps you to take a feed, apply some custom-defined parameters or filtering functions, and generate a valid filtered feed for consumption in a normal RSS reader. In terminal usage this prints the generated feed (pretty-printed)."""

import time
import datetime
import re
import copy
import urllib.parse

import bs4
import feedgenerator
import feedparser

class FeedFetchError(Exception):
    "Thrown when something goes awry fetching or parsing the original feed."
    pass

class FeedFilterError(Exception):
    "Thrown when invalid filter data are given or something blows up when executing filtration."
    pass

def _cast_feed_to_primitives(feed):
    """
    Feedparser's Dicts have lots of peculiar bugs: They present certain keys as
    existing, but they are not poppable, and they don't do **unpacking well. So,
    this converts all the relevant values to dict primitives so they can be expected
    to behave in the usual way. This is called by filter_feed for *all* feeds
    generated by feedparser.
    """
    feed = dict(feed)
    feed['feed'] = dict(feed['feed'])
    for k,v in feed['feed'].items():
        if isinstance(v, feedparser.FeedParserDict):
            feed['feed'][k] = dict(v)
    nuentries = []
    for i in feed['entries']:
        i = dict(i)
        for k,v in i.items():
            if isinstance(v, feedparser.FeedParserDict):
                i[k] = dict(v)
        nuentries.append(i)
    feed['entries'] = nuentries
    return feed

def fetch_and_prepare_feed(feed_url):
    """
    Makes the HTTP requests to get a feed and parses them with feedparser.
    
    Further modifies things so the returned feed doesn't consist of or contain
    feedparser types, to avoid cryptic downstream bugs (looks-like-a-dict-but-acts-funny).
    Also adds a key (if not already there) 'pubdate' to each given RSS item based
    on feedparser's 'published_parsed' struct_time item. 'pubdate' is a datetime.datetime
    object, as required by feedgenerator.
    """
    f = feedparser.parse(feed_url)
    if not f['feed'] or not (200 <= f['status'] < 300):
        raise FeedFetchError()
    f = _cast_feed_to_primitives(f)
    for i in f['entries']:
        if not isinstance(i.get('pubdate', None), datetime.datetime):
            i['pubdate'] = datetime.datetime.fromtimestamp(time.mktime(i['published_parsed']))
    return f

def filter_feed(feed_url, transform_func):
    """
    Fetches a feed using fetch_and_prepare_feed, passes the feed data through
    transform_func (which should make modifications in-place and return the feed),
    then regenerates and returns a feed as a string, pretty-printed using BeautifulSoup.
    """
    f = fetch_and_prepare_feed(feed_url)
    f = transform_func(f)
    if not f['feed'].get('description', None):
        if f['feed']['title']:
            f['feed']['description'] = f['feed']['title']
        elif f['feed']['link']:
            f['feed']['description'] = f['feed']['link']
        else:
            f['feed']['description'] = "Unknown title"
    title = f['feed'].pop('title')
    link = f['feed'].pop('link')
    description = f['feed'].pop('description')
    o = feedgenerator.Rss201rev2Feed(title, link, description, **f['feed'])
    for i in f['entries']:
        if not 'description' in i:
            for alt in ["summary"]:
                if alt in i:
                    i['description'] = i[alt]
        o.add_item( **i )
    feedstr = o.writeString("utf-8")
    feedstr = bs4.BeautifulSoup(feedstr, 'xml').prettify()
    return feedstr

def entry_field_re_filter(re_pattern, key):
    "Returns a filter that allows only entries for which re_pattern yields a match in the chosen key."
    if isinstance(re_pattern, str):
        re_pattern = re.compile(re_pattern)
    def fil(feed):
        nufeed = []
        for i in feed['entries']:
            if re_pattern.search(i[key]):
                nufeed.append(i)
        feed['entries'] = nufeed
        return feed
    return fil
    

def in_url_filter(re_pattern):
    "Returns a filter that filters any entries not matched by the re_pattern in their 'link' fields."
    return entry_field_re_filter(re_pattern, "link")


def in_title_filter(re_pattern):
    "Returns a filter that filters any entries not matched by the re_pattern in their 'title' fields."
    return entry_field_re_filter(re_pattern, "title")


def AND_filter(filter1, filter2):
    "Composes two filters with boolean AND so both must permit an entry for it to make it to the final feed."
    return lambda F: filter1(filter2(F))

def OR_filter(filter1, filter2):
    """
    Return a filter that runs both of the input filters and then combines the
    output feeds (in order without duplication), analogous to boolean OR.
    This operation is more costly than the AND_filter as it copies a lot of values.
    """
    def orfilter(F):
        # Filter a copy to avoid side effects..
        filtered1 = filter1(copy.deepcopy(F))
        filtered2 = filter2(F)
        seen_urls = set()
        entries = []
        for i in filtered1['entries'] + filtered2['entries']:
            if i['link'] in seen_urls: continue
            entries.append(i)
            seen_urls.add(i['link'])
        entries.sort(key=lambda i: i['pubdate'])
        assert len(entries) == len(seen_urls), ("Error: Counted {} urls but"
            " only have {} unique entries!").format(len(seen_urls), len(entries))
        F['entries'] = entries
        return F
    return orfilter

def builtin_main(feed, url_filters=[], title_filters=[], operation="OR"):
    """
    This accepts a feed URL and one-or-more url/title filters, and an operation
    from either OR or AND. It ensures only one or two total filters are given,
    composes them as instructed by operation, and then fetches and filters the
    feed accordingly, returning the filtered, pretty-printed stream.
    The function responsible for terminal usage is _cli_main which just parses
    args and passes them here: breaking these up allows this function to be
    used trivially in a web-app context.
    """
    title_filters = list(map(in_title_filter, title_filters))
    url_filters = list(map(in_url_filter, url_filters))
    both_filters = title_filters + url_filters
    if operation not in ('OR', 'AND'):
        # Error
        raise FeedFilterError("Operation can only be AND or OR: '{}'".format(operation))
    if len(both_filters) == 0:
        raise FeedFilterError("No filters provided!")
    elif len(both_filters) == 1:
        final_filter = both_filters[0]
    elif len(both_filters) == 2:
        if operation == "OR":
            final_filter = OR_filter(*both_filters)
        else:
            final_filter = AND_filter(*both_filters)
    else:
        raise FeedFilterError("Too many filters given; use Regex to re-use some?")
    return filter_feed(feed, final_filter)

def _cli_main():
    import argparse
    P = argparse.ArgumentParser(description=__doc__)
    P.add_argument("source", type=str, help="Target feed to fetch and apply filters to")
    P.add_argument("-u", "--url-filter", type=str, default=[], nargs="+", help="Regex filter(s) to apply to post URLs.")
    P.add_argument("-t", "--title-filter", type=str, default=[], nargs="+", help="Regex filter(s) to apply to post titles.")
    P.add_argument("--operation", type=str, default="OR", help="Whether to use OR (default) or AND operation for multiple filters. Note you can use regex in filters so this is most useful when combining filter types, not filtered values.")
    A = P.parse_args()
    print(builtin_main(A.source, A.url_filter, A.title_filter, A.operation))

if __name__ == "__main__":
    _cli_main()
